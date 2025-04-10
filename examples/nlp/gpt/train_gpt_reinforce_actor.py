# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from functools import partial

import torch
import torch.multiprocessing as mp
from megatron.core.utils import divide
from omegaconf.omegaconf import OmegaConf

from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager
from nemo_aligner.algorithms.reinforce import ReinforceTrainer
from nemo_aligner.data.nlp.builders import (
    build_dataloader,
    build_train_valid_test_rlhf_datasets,
    collate_with_pad_to_max_batch,
)
from nemo_aligner.models.nlp.gpt.megatron_gpt_reinforce_actor import MegatronGPTReinforceActorModel
from nemo_aligner.models.nlp.gpt.reward_critic_clients import RemoteGPTRMClient
from nemo_aligner.utils import parallel_state
from nemo_aligner.utils.batch_iterators import get_batch_iterator_cls
from nemo_aligner.utils.distributed import Timer
from nemo_aligner.utils.train_script_utils import (
    CustomLoggerWrapper,
    add_custom_checkpoint_callback,
    extract_optimizer_scheduler_from_ptl_model,
    init_distributed,
    init_peft,
    init_using_ptl,
    resolve_and_create_trainer,
    retrieve_custom_trainer_state_dict,
)
from nemo_aligner.utils.utils import load_and_override_model_config, load_from_nemo, retrieve_model_state_dict_in_cpu

"""Script to start REINFORCE training"""

OmegaConf.register_new_resolver("multiply", lambda x, y: x * y, replace=True)
OmegaConf.register_new_resolver("int_div", lambda x, y: x // y, replace=True)
OmegaConf.register_new_resolver("subtract", lambda x, y: x - y, replace=True)

mp.set_start_method("spawn", force=True)


@hydra_runner(config_path="conf", config_name="gpt_reinforce_actor")
def main(cfg) -> None:
    cfg.model = load_and_override_model_config(cfg.pretrained_checkpoint.restore_from_path, cfg.model)

    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f"\n{OmegaConf.to_yaml(cfg)}")

    trainer = resolve_and_create_trainer(cfg, "reinforce")

    exp_manager(trainer, cfg.exp_manager)

    logger = CustomLoggerWrapper(trainer.loggers)

    ptl_model = load_from_nemo(
        MegatronGPTReinforceActorModel,
        cfg.model,
        trainer,
        strict=True,
        restore_path=cfg.pretrained_checkpoint.restore_from_path,
    )

    init_peft(ptl_model, cfg.model)

    init_policy_state_dict = None

    # only need this if we are running with inital kl penalty & full-parameter tuning
    if cfg.trainer.reinforce.initial_policy_kl_penalty > 0 and cfg.model.peft.peft_scheme == "none":
        init_policy_state_dict = retrieve_model_state_dict_in_cpu(
            ptl_model, megatron_amp_O2=cfg.model.get("megatron_amp_O2", False)
        )

    ptl_model.init_policy_state_dict = init_policy_state_dict

    # pull values from checkpoint
    trainer_restore_path = trainer.ckpt_path

    # TODO: log this restore path
    if trainer_restore_path is not None:
        custom_trainer_state_dict = retrieve_custom_trainer_state_dict(trainer)
    else:
        custom_trainer_state_dict = None

    init_distributed(trainer, ptl_model, cfg.model.get("transformer_engine", False))

    # use the entire dataset
    train_valid_test_num_samples = [-1, -1, -1]
    train_ds, validation_ds, _ = build_train_valid_test_rlhf_datasets(
        cfg=cfg.model,
        data_prefix=cfg.model.data.data_prefix,
        data_impl=cfg.model.data.data_impl,
        splits_string=cfg.model.data.splits_string,
        train_valid_test_num_samples=train_valid_test_num_samples,
        seq_length=cfg.model.data.seq_length,
        seed=cfg.model.seed,
        tokenizer=ptl_model.tokenizer,
    )

    max_seqlen = cfg.model.reinforce.length_params.max_length
    eos_id = ptl_model.tokenizer.eos_id

    # collate fn to pad to the max seq length in the batch
    collate_fn = collate_with_pad_to_max_batch(max_seqlen, eos_id, cfg, generate_masks_and_position_ids=False)

    train_dataloader_builder = partial(
        build_dataloader,
        cfg=cfg,
        dataset=train_ds,
        mbs=cfg.model.reinforce.rollout_micro_batch_size,
        gbs=cfg.model.reinforce.num_rollout_samples,
        collate_fn=collate_fn,
        load_gbs=False,
    )

    val_dataloader_builder = partial(
        build_dataloader,
        cfg=cfg,
        dataset=validation_ds,
        mbs=cfg.model.reinforce.val_rollout_micro_batch_size,
        gbs=cfg.model.reinforce.num_val_samples,
        collate_fn=collate_fn,
        load_gbs=False,
        use_random_sampler=False,
    )

    # nemo uses the train dataloader to figure out
    # max steps to take when max_steps = -1
    # but our train dataloader is for the prompts
    # so we instaniate a dummy dataloader
    # to get the proper max *optimization* steps
    # nemo treats batch size of normal dataloader as GBS/DP
    # so we need to offset it by DP
    dummy_train_dataloader = torch.utils.data.DataLoader(
        dataset=train_ds, batch_size=divide(cfg.model.global_batch_size, parallel_state.get_data_parallel_world_size())
    )

    init_using_ptl(trainer, ptl_model, dummy_train_dataloader, train_ds)
    # make sure the dummy train dataloader is never used
    del ptl_model._train_dl
    del dummy_train_dataloader

    optimizer, scheduler = extract_optimizer_scheduler_from_ptl_model(ptl_model)
    ckpt_callback = add_custom_checkpoint_callback(trainer, ptl_model)

    logger.log_hyperparams(OmegaConf.to_container(cfg))

    rm = RemoteGPTRMClient(cfg.remote_rm)
    timer = Timer(cfg.exp_manager.get("max_time_per_run") if cfg.exp_manager else None)

    batch_iterator_cfg = cfg.trainer.reinforce.get("batch_iterator", {})
    batch_iterator_cls = get_batch_iterator_cls(batch_iterator_cfg)

    reinforce_trainer = ReinforceTrainer(
        cfg=cfg.trainer.reinforce,
        model=ptl_model,
        optimizer=optimizer,
        scheduler=scheduler,
        train_dataloader_builder=train_dataloader_builder,
        val_dataloader_builder=val_dataloader_builder,
        collate_fn=collate_fn,
        rm=rm,
        batch_iterator_cls=batch_iterator_cls,
        logger=logger,
        ckpt_callback=ckpt_callback,
        run_timer=timer,
    )

    if custom_trainer_state_dict is not None:
        reinforce_trainer.load_state_dict(custom_trainer_state_dict)

    reinforce_trainer.fit()

    # Note: The main loop creates multiple HTTPCommunicators which own a
    # pytriton.client.FuturesModelClient. At the end of the loop, we manually
    # close all FuturesModelClients since we do not use the context manager
    # syntax. This guarantees all dangling threads are no longer blocking.
    # `atexit` does not suffice since the registered cleanup function can be
    # queued behind another blocking atexit registered function.
    # TODO: utilize context managers to avoid manual cleanup
    rm.communicator.close()


if __name__ == "__main__":
    main()
