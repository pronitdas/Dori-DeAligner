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

import math
from collections import defaultdict
from statistics import mean
from typing import Any, Protocol

import torch
import torch.distributed
from omegaconf.dictconfig import DictConfig
from tqdm import tqdm

from nemo.collections.nlp.data.language_modeling.megatron.megatron_batch_samplers import (
    MegatronPretrainingRandomBatchSampler,
)
from nemo.collections.nlp.modules.common.megatron.utils import get_ltor_masks_and_position_ids
from nemo.utils import logging
from nemo_aligner.utils import parallel_state
from nemo_aligner.utils.distributed import SyncTimer
from nemo_aligner.utils.train_utils import clip_gradients
from nemo_aligner.utils.trainer_utils import check_progress, compute_limit_batches, compute_num_steps_per_epoch
from nemo_aligner.utils.utils import clear_memory


class DistributedCollateFunction(Protocol):
    def __call__(self, batch: list[dict], **kwargs: Any) -> dict[str, torch.Tensor]:
        ...


def dpo_custom_collate(
    batch: list[dict],
    eos_id: int,
    reset_position_ids: bool = False,
    reset_attention_mask: bool = False,
    eod_mask_loss: bool = False,
    pad_length_to_multiple_of: int | None = None,
) -> dict[str, torch.Tensor]:
    """
    Transposes minibatch from list[dict] -> dict[Tensor] and also pads
    
    This collate happens outside of the torch data loader and is not compatible with the multiprocessing
    logic due to requiring communication collectives.
    """
    if pad_length_to_multiple_of is not None and pad_length_to_multiple_of < 0:
        raise ValueError(f"{pad_length_to_multiple_of=} must be >= 0")
    chosen_tokens = [item["chosen"] for item in batch]
    rejected_tokens = [item["rejected"] for item in batch]
    chosen_lengths = torch.LongTensor([item["chosen_length"] for item in batch])
    rejected_lengths = torch.LongTensor([item["rejected_length"] for item in batch])
    chosen_labels = [item["chosen_labels"] for item in batch]
    rejected_labels = [item["rejected_labels"] for item in batch]
    chosen_rewards = torch.FloatTensor([item["chosen_reward"] for item in batch])
    rejected_rewards = torch.FloatTensor([item["rejected_reward"] for item in batch])

    chosen_tokens = torch.nn.utils.rnn.pad_sequence(chosen_tokens, batch_first=True, padding_value=eos_id)
    rejected_tokens = torch.nn.utils.rnn.pad_sequence(rejected_tokens, batch_first=True, padding_value=eos_id)
    chosen_labels = torch.nn.utils.rnn.pad_sequence(chosen_labels, batch_first=True, padding_value=-100)
    rejected_labels = torch.nn.utils.rnn.pad_sequence(rejected_labels, batch_first=True, padding_value=-100)
    assert chosen_tokens.shape == rejected_tokens.shape
    assert chosen_labels.shape == rejected_labels.shape

    if pad_length_to_multiple_of:
        # Assumes both chosen and rejected match
        max_seq_len = torch.tensor(chosen_tokens.shape[1], device=torch.cuda.current_device())
        torch.distributed.all_reduce(
            max_seq_len, op=torch.distributed.ReduceOp.MAX, group=parallel_state.get_data_parallel_group()
        )

        padded_max_len = math.ceil(max_seq_len / pad_length_to_multiple_of) * pad_length_to_multiple_of
        chosen_tokens = torch.nn.functional.pad(
            chosen_tokens, (0, padded_max_len - chosen_tokens.shape[1]), mode="constant", value=eos_id
        )
        rejected_tokens = torch.nn.functional.pad(
            rejected_tokens, (0, padded_max_len - rejected_tokens.shape[1]), mode="constant", value=eos_id
        )
        chosen_labels = torch.nn.functional.pad(
            chosen_labels, (0, padded_max_len - chosen_labels.shape[1]), mode="constant", value=-100
        )
        rejected_labels = torch.nn.functional.pad(
            rejected_labels, (0, padded_max_len - rejected_labels.shape[1]), mode="constant", value=-100
        )

    attention_mask, _, position_ids = get_ltor_masks_and_position_ids(
        chosen_tokens.cuda(), eos_id, reset_position_ids, reset_attention_mask, eod_mask_loss,
    )
    assert attention_mask.ndim == 4, "attention_mask is incorrect shape for dpo_custom_collate"
    if attention_mask.shape[0] == 1:
        # using .expand() here causes errors from pin_memory=True, so need to use .repeat()
        # attention_mask = attention_mask.expand(len(batch), *((-1,) * (len(attention_mask.shape) - 1)))
        attention_mask = attention_mask.repeat(len(batch), *((1,) * (len(attention_mask.shape) - 1)))

    output = {
        "chosen": chosen_tokens,
        "rejected": rejected_tokens,
        "chosen_length": chosen_lengths,
        "rejected_length": rejected_lengths,
        "chosen_labels": chosen_labels,
        "rejected_labels": rejected_labels,
        "attention_mask": attention_mask,
        "position_ids": position_ids,
        "chosen_rewards": chosen_rewards,
        "rejected_rewards": rejected_rewards,
    }
    return output


class DPOTrainer:
    """Trainer to coordinate DPO training"""

    def __init__(
        self,
        cfg: DictConfig,
        model,
        optimizer,
        scheduler,
        train_dataloader,
        val_dataloader,
        test_dataloader,
        collate_fn: DistributedCollateFunction,
        logger,
        ckpt_callback,
        run_timer,
    ):
        self.model = model
        self.train_dataloader = train_dataloader
        self.val_dataloader = val_dataloader
        self.test_dataloader = test_dataloader
        self.collate_fn = collate_fn
        self.logger = logger
        self.cfg = cfg
        self.optimizer = optimizer
        self.scheduler = scheduler

        # this timer checks if we should stop training
        self.run_timer = run_timer

        self.step = 0
        self.consumed_samples = 0

        self.ckpt_callback = ckpt_callback

        # compute `max_steps`
        self.num_steps_per_epoch = compute_num_steps_per_epoch(
            self.train_dataloader.batch_sampler, self.cfg.get("limit_train_batches", 1.0)
        )

        self.limit_val_batches = compute_limit_batches(len(val_dataloader), self.cfg.limit_val_batches)
        self.val_check_interval = (
            int(self.cfg.val_check_interval * self.num_steps_per_epoch)
            if isinstance(self.cfg.val_check_interval, float)
            else self.cfg.val_check_interval
        )
        self.set_max_steps()

        self.timer = SyncTimer(
            reduction="mean", sync_cuda=True, buffer_size=1, reduce_op=torch.distributed.ReduceOp.MAX
        )

    def validation_step(self, global_batch):
        # these things should go into a GPTModel wrapper
        self.model.prepare_for_validation_step()

        loss_mean, metrics = self.model.get_loss_and_metrics(batch=global_batch, forward_only=True)

        self.model.finish_validation_step()
        return loss_mean, metrics

    @torch.no_grad()
    def run_validation(self):
        loss_means = []
        val_metrics = defaultdict(list)

        val_pbar = tqdm(
            zip(range(self.limit_val_batches), self.augment_dataloader(self.val_dataloader)),
            total=self.limit_val_batches,
            leave=True,
            desc="Validation steps",
        )

        for _, batch in val_pbar:
            self.timer.start("validation_step_time")
            loss_mean, metrics = self.validation_step(batch)
            self.timer.stop("validation_step_time")
            validation_step_time = self.timer.get("validation_step_time")

            metrics["validation_step_time"] = validation_step_time

            loss_means.append(loss_mean)
            for k, v in metrics.items():
                val_metrics[k].append(v)
            log_val_metrics = {f"val_{k}": v for k, v in metrics.items()}
            val_pbar.set_postfix(log_val_metrics)

        val_metrics = {k: mean(v) for k, v in val_metrics.items()}
        return mean(loss_means), val_metrics

    def train_single_step(self, global_batch):
        self.optimizer.zero_grad()

        self.model.prepare_for_training_step()

        # NOTE: assume backward is called on the loss already
        loss_mean, metrics = self.model.get_loss_and_metrics(batch=global_batch, forward_only=False)

        self.model.finish_training_step()

        grad_norm = clip_gradients(self.model, self.cfg.gradient_clip_val)
        grad_norm = grad_norm.item() if torch.is_tensor(grad_norm) else grad_norm
        lr = self.optimizer.param_groups[0]["lr"]

        self.optimizer.step()
        self.scheduler.step()

        trainer_metrics = {}
        if grad_norm is not None:
            trainer_metrics["grad_norm"] = grad_norm
        trainer_metrics.update({"lr": lr, "loss": loss_mean})

        return loss_mean, {**metrics, **trainer_metrics}

    def fit(self):
        if (not isinstance(self.train_dataloader.batch_sampler, MegatronPretrainingRandomBatchSampler)) and (
            self.cfg.max_epochs is not None and self.cfg.max_epochs > 1
        ):
            # if you use MegatronPretrainingBatchSampler as the batch_sampler passed to your train dataloader (in builders.py)
            # then each epoch will repeat all your samples in the same order as the previous epoch, there is no shuffling
            # to fix this, you should use MegatronPretrainingRandomBatchSampler instead, which alleviates this issue and allows
            # random shuffling for each epoch.
            raise ValueError(
                "max_epochs > 1 is not supported unless using `MegatronPretrainingRandomBatchSampler` as the batch_sampler for your train dataloader"
            )

        epoch_iter = range(self.epoch, self.cfg.max_epochs)
        if len(epoch_iter) <= 0:
            # epoch done
            return

        self.run_timer.start_time()

        for _ in epoch_iter:
            num_steps_in_epoch = min(
                self.max_steps - self.step, self.num_steps_per_epoch - self.step % self.num_steps_per_epoch
            )
            loop_iter = range(num_steps_in_epoch)

            if not loop_iter:
                return  # training ended

            global_pbar = tqdm(
                self.augment_dataloader(self.train_dataloader),
                initial=self.step,
                total=self.max_steps,
                leave=True,
                desc="Training steps",
            )

            for _, global_batch in zip(loop_iter, global_pbar):
                self.timer.start("train_step_time")
                loss, metrics = self.train_single_step(global_batch)
                self.timer.stop("train_step_time")
                train_step_time = self.timer.get("train_step_time")
                # to help avoid fragmentation
                clear_memory()

                # TODO(geshen): maybe use the dataloader instead
                # bump up the consumed samples but not the step
                self.consumed_samples += self.model.cfg.global_batch_size
                metrics["consumed_samples"] = self.consumed_samples
                metrics["step_time"] = train_step_time
                metrics["epoch"] = self.epoch + 1
                self.logger.log_metrics(
                    metrics, step=self.step, prefix="train/",
                )
                metrics = {f"train_{k}": v for k, v in metrics.items()}

                self.step += 1

                run_time_exceeded = self.run_timer.is_finished()
                run_val, save_model, is_train_end = check_progress(
                    self.step,
                    self.max_steps,
                    self.val_check_interval,
                    self.cfg.save_interval,
                    self.limit_val_batches,
                    run_time_exceeded=run_time_exceeded,
                )

                if run_val:
                    val_loss, val_metrics = self.run_validation()
                    # validation is done on the UPDATED weights
                    # so we use the incremented self.step
                    self.logger.log_metrics(val_metrics, step=self.step, prefix="val/")
                    val_metrics = {f"val_{k}": v for k, v in val_metrics.items()}
                    metrics.update(val_metrics)

                global_pbar.set_postfix(metrics)

                if save_model:
                    # PTL save wants tensors only
                    metrics = {k: torch.as_tensor(v) for k, v in metrics.items()}
                    self.save(metrics, is_train_end=is_train_end)

                if run_time_exceeded:
                    logging.info(f"Time limit given by run_timer={self.run_timer} reached. Stopping run")
                    return

                metrics.clear()

        self.logger.finalize()

    def save(self, extra_candidates=None, is_train_end=False):
        """PTL based save"""
        torch.distributed.barrier()

        if extra_candidates is None:
            extra_candidates = {}

        monitor_candidates = {k: torch.tensor(v, dtype=torch.int32) for k, v in self.state_dict().items()}
        monitor_candidates.update(extra_candidates)

        self.ckpt_callback.custom_save(monitor_candidates=monitor_candidates, is_train_end=is_train_end)

    def set_max_steps(self):
        self.max_steps = self.num_steps_per_epoch * self.cfg.max_epochs

        if (max_steps := self.cfg.get("max_steps", -1)) >= 0:
            self.max_steps = min(self.max_steps, max_steps)

    def state_dict(self):
        return {
            "step": self.step,
            "consumed_samples": self.consumed_samples,
            "epoch": self.epoch,
        }

    def load_state_dict(self, state_dict):
        self.step = state_dict["step"]
        self.consumed_samples = state_dict["consumed_samples"]

        loaded_values = [self.step, self.consumed_samples]

        # make sure everyone loaded the same checkpoint as rank 0
        to_broadcast = torch.tensor(loaded_values, dtype=torch.float32, device=torch.cuda.current_device())
        torch.distributed.broadcast(to_broadcast, 0)

        assert loaded_values == to_broadcast.tolist()
        # restore max steps we need to run for
        self.set_max_steps()

    def augment_dataloader(self, dataloader):
        """Augment dataloader with ref policy log prob"""
        iter_dataloader = iter(dataloader)
        while True:
            try:
                batch = next(iter_dataloader)
                batch = self.collate_fn(batch)
                logprobs = self.model.get_ref_policy_logprobs(batch).cpu()
                packed = "input_ids" in batch
                if not packed:
                    chosen_logps, reject_logps = torch.split(logprobs, len(logprobs) // 2, dim=0)
                    batch["ref_policy_log_probs_chosen"] = chosen_logps
                    batch["ref_policy_log_probs_rejected"] = reject_logps
                else:
                    batch["ref_policy_log_probs"] = logprobs

                yield batch
                del logprobs
                if not packed:
                    del chosen_logps, reject_logps
            except StopIteration:
                break

    @property
    def epoch(self):
        return self.step // self.num_steps_per_epoch
