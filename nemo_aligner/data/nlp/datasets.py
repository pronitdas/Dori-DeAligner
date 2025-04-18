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

"""Custom datasets for RLHF training"""

import math
import os
from typing import Dict, List

import numpy as np
import scipy
import torch
from omegaconf import OmegaConf

from nemo.collections.nlp.data.language_modeling.megatron.gpt_dataset import _create_ltor_masks_and_position_ids
from nemo.collections.nlp.data.language_modeling.megatron.gpt_sft_chat_dataset import (
    GPTSFTChatDataset,
    _get_header_conversation_type_mask_role,
    get_prompt_template_example,
)
from nemo.core import Dataset
from nemo.utils import logging
from nemo_aligner.utils import parallel_state


class KnowledgeDistillationDataset(Dataset):
    """The knowledge distillation dataset takes in raw tokens, labels, loss masks, and the teacher models' predictive top tokens & logits.
    """

    def __init__(
        self, cfg, tokenizer, name, data_prefix, documents, data, seq_length, seed, drop_last=True,
    ):
        super().__init__()
        self.cfg = cfg
        self.name = name
        self.data = data
        self.seq_length = seq_length

        self.nograd_length = 32

        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < len(self.data)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        """Returns a SFT example with topk_logits, topk_token_ids and target log_sum_exp_logits
        """
        payload = self.data[idx]
        for key in ["tokens", "labels", "loss_mask", "topk_token_ids"]:
            assert key in payload, f"{key} not in the data"
            payload[key] = torch.tensor(payload[key], dtype=torch.int64)
        for key in ["topk_logits", "log_sum_exp_logits"]:
            assert key in payload, f"{key} not in the data"
            payload[key] = torch.tensor(payload[key], dtype=torch.float32)

        if self.cfg.data.top_k is not None:
            payload["topk_logits"] = payload["topk_logits"][..., : self.cfg.data.top_k]
            payload["topk_token_ids"] = payload["topk_token_ids"][..., : self.cfg.data.top_k]

        length = len(payload["tokens"])
        if length > self.seq_length:
            logging.warning(
                f"WARNING: Tokenized text exceeds max seq length ({length} vs {self.seq_length})."
                + f"The example will be ignored."
            )
            # ignore the example whose tokenized text exceeds max seq length.
            for key in ["tokens", "labels", "topk_logits", "topk_token_ids", "log_sum_exp_logits"]:
                payload[key] = payload[key][
                    : self.nograd_length
                ]  ## make dummy example very short to reduce computation
            payload["loss_mask"] = torch.zeros_like(payload["tokens"])

        return payload


class RLHFDataset(Dataset):
    def __init__(
        self, cfg, tokenizer, name, data_prefix, documents, data, seq_length, seed, drop_last=True,
    ):
        super().__init__()
        self.cfg = cfg
        self.name = name
        self.data = data
        self.drop_last = drop_last
        self.seq_length = seq_length
        self.tokenizer = tokenizer

        if "length_params" in cfg:
            max_sample_length = seq_length - cfg.length_params.max_length
        else:
            max_sample_length = seq_length // 2

        assert max_sample_length > 0, f"max sample length must be greater than 0, but got {max_sample_length}"

        self.max_sample_length = max_sample_length

        self.use_json = self.cfg.data.data_impl.startswith("json")

        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < len(self.data)

        # save index mappings to a configurable dir
        self.index_mapping_dir = cfg.data.get("index_mapping_dir", None)

        # create index_mapping_dir on rank 0
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            if torch.distributed.get_rank() == 0:
                if self.index_mapping_dir is not None and not os.path.isdir(self.index_mapping_dir):
                    os.makedirs(self.index_mapping_dir)
            torch.distributed.barrier()

    def __len__(self):
        return len(self.data)

    def encode(self, text):
        if self.cfg.data.get("apply_ftfy", False):
            import ftfy

            text = ftfy.fix_text(text)

        text_ids = self.tokenizer.text_to_ids(text)

        if len(text_ids) > 0 and self.cfg.data.get("append_eod", False):
            text_ids.append(self.tokenizer.eos_id)

        return text_ids, len(text_ids)

    def __getitem__(self, idx):
        """
        Return a single prompt.
        """
        mask_sample = False
        if idx == -1:
            # This may happen on the last batch due to the padding that occurs in
            #   https://github.com/NVIDIA/NeMo/blob/643d814fc2d885b7348ac676333ebd76cd79b663/nemo/collections/nlp/data/language_modeling/megatron/megatron_batch_samplers.py#L168
            # in which case we may want to mask the loss associated to these padded samples.
            # However, this class is not currently used, so for now we raise an exception: this may be revisited
            # at a later time if this situation actually occurs in practice.
            # logging.warning("Got -1 as item index in RLHFDataset => masking loss from this sample")
            raise NotImplementedError("Obtained unexpected `idx == -1`, see comments in code for details")

        orig_idx = idx = idx % len(self)
        while True:
            sample = self.data[idx]
            if self.use_json:
                sample, _ = self.encode(sample["text"])
            if len(sample) <= self.max_sample_length:
                break
            idx = (idx + 1) % len(self)
            if idx == orig_idx:
                raise RuntimeError(f"All samples have length > {self.max_sample_length}")
            continue

        if idx != orig_idx:
            logging.warning(
                f"Sample {orig_idx} in dataset '{self.name}' has length "
                f"{len(self.data[orig_idx])} > {self.max_sample_length} "
                f"=> replacing it with sample {idx} and masking its loss"
            )
            mask_sample = True

        if self.use_json:
            # `sample` is a regular Python list.
            sample_tensor = torch.tensor(sample, dtype=torch.int64)
        else:
            # `sample` is a NumPy array.
            sample_tensor = torch.from_numpy(sample.astype(np.int64))

        # if we want to mask the sample we should
        # set the loss multiplier to 0
        loss_multiplier = not mask_sample

        output = {
            "text": sample_tensor,
            "length": sample_tensor.shape[0],
            "loss_multiplier": loss_multiplier,
        }
        return output


class RewardModelDataset(Dataset):
    """This class assumes that we only have 2 responses per prompt that is ranked. Chosen is the better
    one(even index) whereas Rejected is the worse response(odd index)
    """

    def __init__(
        self, cfg, tokenizer, name, data_prefix, documents, data, seq_length, seed, drop_last=True,
    ):
        super().__init__()
        self.cfg = cfg
        self.name = name
        self.data = data
        self.drop_last = drop_last
        self.seq_length = seq_length
        self.tokenizer = tokenizer

        self.reset_position_ids = cfg.data.get("reset_position_ids", False)
        self.reset_attention_mask = cfg.data.get("reset_attention_mask", False)
        self.eod_mask_loss = cfg.data.get("eod_mask_loss", False)
        self.eos_id = tokenizer.eos_id

        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < len(self.data)

        # save index mappings to a configurable dir
        self.index_mapping_dir = cfg.data.get("index_mapping_dir", None)

        # create index_mapping_dir on rank 0
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            if torch.distributed.get_rank() == 0:
                if self.index_mapping_dir is not None and not os.path.isdir(self.index_mapping_dir):
                    os.makedirs(self.index_mapping_dir)
            torch.distributed.barrier()

    def __len__(self):
        return len(self.data) // 2

    def encode(self, text):
        if self.cfg.data.get("apply_ftfy", False):
            import ftfy

            text = ftfy.fix_text(text)

        text_ids = self.tokenizer.text_to_ids(text)

        if len(text_ids) > 0 and self.cfg.data.get("append_eod", False):
            text_ids.append(self.tokenizer.eos_id)

        return text_ids, len(text_ids)

    def __getitem__(self, idx, multiple=2):
        """Returns a pair of chosen/rejected pairs, and their respective lengths."""
        found = False
        while not found:
            chosen = self.data[multiple * idx]
            rejected = self.data[multiple * idx + 1]
            if self.cfg.data.data_impl.startswith("json"):
                chosen, _ = self.encode(chosen["text"])
                rejected, _ = self.encode(rejected["text"])
            if len(chosen) > self.seq_length or len(rejected) > self.seq_length:
                idx += multiple
                continue
            found = True

        # in the future, we should pad to the max seq len of the mini-batch instead of model.seq_length
        # max_curr_seq_len = max(len(chosen), len(rejected))

        chosen_np = np.array(chosen, dtype=np.int64)
        chosen_np_pad = np.pad(
            chosen_np, (0, max(0, self.seq_length - chosen_np.shape[0])), mode="constant", constant_values=self.eos_id
        )
        rejected_np = np.array(rejected, dtype=np.int64)
        rejected_np_pad = np.pad(
            rejected_np,
            (0, max(0, self.seq_length - rejected_np.shape[0])),
            mode="constant",
            constant_values=self.eos_id,
        )

        chosen_tokens = torch.tensor(chosen_np_pad)
        rejected_tokens = torch.tensor(rejected_np_pad)

        attention_mask, loss_mask, position_ids = _create_ltor_masks_and_position_ids(
            chosen_tokens, self.eos_id, self.reset_position_ids, self.reset_attention_mask, self.eod_mask_loss,
        )

        # Negative index comes when we pad the last batch in MegatronPretrainingBatchSampler
        # We make the loss_mask zero to mask out loss from these samples
        if idx == -1:
            logging.info("WARNING: Got -1 as item index. Masking loss from this sample")
            loss_mask = torch.zeros_like(loss_mask)

        output = {
            "chosen": chosen_tokens,
            "rejected": rejected_tokens,
            "chosen_length": chosen_np.shape[0],
            "rejected_length": rejected_np.shape[0],
            "attention_mask": attention_mask,
            "loss_mask": loss_mask,
            "position_ids": position_ids,
        }
        return output


class DPOModelDataset(Dataset):
    """This class works only with jsonl files. It assumes each line of the json file is a dictionary
    with the prompt, along with the chosen response (response only, no prompt), and the rejected response
    (response only, no prompt). This Dataset will combine the prompt with each corresponding chosen and
    rejected response, and then tokenize it. It also returns the labels for each, which is the response tokens
    with -100 for the prompt part.

    WARNING: This class will tokenize the text, but it will raise an exception on model max seq len violations!
             Meaning it will not truncate tokens to fit to model max seq len, because of special prefix/suffix
             strings such as <extra_id_1>, it would not know where it is safe to truncate for each model. Therefore,
             the user must do all truncation logic in their preprocessing step when generating the jsonl
             used by this class. Put all special truncation logic there specific to your model.
    """

    def __init__(
        self,
        cfg,
        tokenizer,
        name,
        data_prefix,
        documents,
        data,
        seq_length,
        seed,
        drop_last=True,
        pad_chosen_rejected_to_max=True,
    ):
        super().__init__()
        self.cfg = cfg
        self.name = name
        self.data = data
        self.drop_last = drop_last
        self.seq_length = seq_length
        self.tokenizer = tokenizer

        ## pad_chosen_rejected_to_max should be true unless iterating through the
        ## dataset as a data preparation step for packing
        self.pad_chosen_rejected_to_max = pad_chosen_rejected_to_max

        self.reset_position_ids = cfg.data.get("reset_position_ids", False)
        self.reset_attention_mask = cfg.data.get("reset_attention_mask", False)
        self.eod_mask_loss = cfg.data.get("eod_mask_loss", False)
        self.eos_id = tokenizer.eos_id
        self.default_chosen_reward = cfg.data.get("default_chosen_reward", 1.0)
        self.default_rejected_reward = cfg.data.get("default_rejected_reward", 0.0)

        self.nograd_length = 32

        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < len(self.data)

    def __len__(self):
        return len(self.data)

    def encode(self, text, append_eod=False):
        if self.cfg.data.get("apply_ftfy", False):
            import ftfy

            text = ftfy.fix_text(text)

        text_ids = self.tokenizer.text_to_ids(text)

        if len(text_ids) > 0 and append_eod:
            text_ids.append(self.tokenizer.eos_id)

        return text_ids, len(text_ids)

    @staticmethod
    def _convert_messages(
        input_list: List[Dict[str, str]]
    ) -> Dict:  # TODO: (@adithyare) this method should live elsewhare..
        """
        args:
            input_list: is a list of dicts in the openai format
                for example:
                [{"role": "system", "content": "you are helpful},
                {"role": "user", "content": "Why is the sky blue?"},
                {"role": "assistant", "content": "Because blablabla"},
                ...]
        returns:
            output_dict: a dict in nemo's format {"system": "sytem prompt",
                                                 "conversation": [],
                                                 ...
                                                } 
        """
        output_dict = {
            "system": "",
            "conversations": [],
            "mask": "User",
            "type": "VALUE_TO_TEXT",
        }

        # Extract the system message
        num_system_msg = 0
        for msg in input_list:
            if msg["role"] == "system":
                output_dict["system"] = msg["content"]
                num_system_msg += 1
            if num_system_msg > 1:
                raise RuntimeError("Multiple system messages seen, please consolidate into a single system message.")

        # Build the conversations list
        for msg in input_list:
            if msg["role"] != "system":
                conversation_entry = {
                    "from": msg["role"].capitalize(),  # Capitalize 'user' and 'assistant'
                    "value": msg["content"],
                    "label": None,
                }
                output_dict["conversations"].append(conversation_entry)

        return output_dict

    def convert(self, messages):
        """
        args:
            messages: is a list of dicts in the openai format
                for example:
                [{"role": "system", "content": "you are helpful},
                {"role": "user", "content": "Why is the sky blue?"},
                {"role": "assistant", "content": "Because blablabla"},
                ...]
        returns:
            conversation:  is a string formatted with the chat template
        """
        if OmegaConf.select(self.cfg, "data.chat_prompt_tokens") is None:
            raise RuntimeError(
                "You don't have a model (model_config.yaml) which has chat_prompt_tokens, are you sure this is a Chat/Instruction model?"
            )
        special_tokens = self.cfg.data.chat_prompt_tokens
        nemo_source = self._convert_messages(messages)
        header, conversation, data_type, mask_role = _get_header_conversation_type_mask_role(
            nemo_source, special_tokens
        )
        return conversation

    def __getitem__(self, idx):
        """Returns a pair of chosen/rejected pairs, their respective lengths, and labels."""
        payload = self.data[idx]

        if isinstance(payload["prompt"], str):
            # (@adithyare) format with hardcoded chat tokens
            # will allow this for the time being.
            prompt_fmtd = payload["prompt"]
            chosen_fmtd = payload["prompt"] + payload["chosen_response"]
            rejected_fmtd = payload["prompt"] + payload["rejected_response"]
        else:
            prompt_fmtd = self.convert(payload["prompt"])  # (@adithyare) read var as "prompt formatted"
            chosen_fmtd = self.convert(payload["prompt"] + [payload["chosen_response"]])
            rejected_fmtd = self.convert(payload["prompt"] + [payload["rejected_response"]])

        prompt, prompt_len = self.encode(prompt_fmtd, append_eod=False)
        chosen, chosen_len = self.encode(chosen_fmtd, append_eod=self.cfg.data.get("append_eod", False))
        reject, reject_len = self.encode(rejected_fmtd, append_eod=self.cfg.data.get("append_eod", False))

        # chosen_response_only, chosen_response_len = self.encode(payload['chosen_response'])
        # reject_response_only, reject_response_len = self.encode(payload['rejected_response'])
        chosen_labels = ([-100] * prompt_len) + chosen[prompt_len:]
        reject_labels = ([-100] * prompt_len) + reject[prompt_len:]

        assert (
            chosen[0:prompt_len] == prompt
        ), f"The tokenizer for DPO has merged tokens between prompt and response for {idx=}:\n[[prompt]]={repr(payload['prompt'])}\n[[chosen_response]]={repr(payload['chosen_response'])}"
        assert (
            reject[0:prompt_len] == prompt
        ), f"The tokenizer for DPO has merged tokens between prompt and response for {idx=}:\n[[prompt]]={repr(payload['prompt'])}\n[[rejected_response]]={repr(payload['rejected_response'])}"

        max_curr_seq_len = max(chosen_len, reject_len)

        if self.pad_chosen_rejected_to_max:
            chosen_tokens = torch.nn.functional.pad(
                torch.LongTensor(chosen), (0, max_curr_seq_len - chosen_len), mode="constant", value=self.eos_id
            )
            rejected_tokens = torch.nn.functional.pad(
                torch.LongTensor(reject), (0, max_curr_seq_len - reject_len), mode="constant", value=self.eos_id
            )
            labels_chosen_tokens = torch.nn.functional.pad(
                torch.LongTensor(chosen_labels),
                (0, max_curr_seq_len - len(chosen_labels)),
                mode="constant",
                value=-100,
            )
            labels_reject_tokens = torch.nn.functional.pad(
                torch.LongTensor(reject_labels),
                (0, max_curr_seq_len - len(reject_labels)),
                mode="constant",
                value=-100,
            )
        else:
            chosen_tokens = torch.LongTensor(chosen)
            rejected_tokens = torch.LongTensor(reject)
            labels_chosen_tokens = torch.LongTensor(chosen_labels)
            labels_reject_tokens = torch.LongTensor(reject_labels)

        ignore_example = False
        # ignore the example whose tokenized text exceeds max seq length.
        if max_curr_seq_len > self.seq_length:
            logging.warning(
                f"WARNING: Tokenized text exceeds max seq length ({max_curr_seq_len} vs {self.seq_length})."
                + f"The example will be ignored."
            )
            chosen_tokens = chosen_tokens[: self.nograd_length]
            rejected_tokens = rejected_tokens[: self.nograd_length]
            labels_chosen_tokens = torch.ones_like(chosen_tokens) * (-100)
            labels_reject_tokens = torch.ones_like(rejected_tokens) * (-100)
            chosen_len = self.nograd_length
            reject_len = self.nograd_length
            ignore_example = True

        output = {
            "chosen": chosen_tokens,
            "rejected": rejected_tokens,
            "chosen_length": chosen_len,
            "rejected_length": reject_len,
            "chosen_labels": labels_chosen_tokens,
            "rejected_labels": labels_reject_tokens,
            "chosen_reward": payload.get("chosen_reward", self.default_chosen_reward),
            "rejected_reward": payload.get("rejected_reward", self.default_rejected_reward),
            "ignore_example": ignore_example,
        }
        return output


class DPOPackedDataset(DPOModelDataset):
    """A dataset class for DPO with sequence packing. Data is expected to be 
    pre-tokenized and pre-packed using examples/nlp/data/dpo/prepare_packed_dpo_dataset.py.
    """

    REWARDS_PAD_ID = -1000
    LABELS_PAD_ID = -100

    def __init__(
        self,
        cfg,
        tokenizer,
        name,
        data_prefix,
        documents,
        data,
        seq_length,
        seed,
        drop_last=True,  # return_cu_seqlen: bool = True ## should always be true
    ):

        super().__init__(cfg, tokenizer, name, data_prefix, documents, data, seq_length, seed, drop_last)
        self.data_prefix = data_prefix

    def __getitem__(self, idx):
        return self.data[idx]

    def _ceil_to_nearest(self, n, m):
        return (n + m - 1) // m * m

    def _maybe_cast_to_list(self, x):
        return [item.tolist() if isinstance(item, np.ndarray) else item for item in x]

    def _collate_item(self, item, max_length, pad_id):
        item = self._maybe_cast_to_list(item)
        item = [x + [pad_id] * (max_length - len(x)) for x in item]
        return item

    ## reset_position_ids, reset_attention_mask and eod_mask_loss are unused but are needed to match the API of dpo_custom_collate
    def global_collate_fn(
        self,
        batch,
        eos_id,
        reset_position_ids=False,
        reset_attention_mask=False,
        eod_mask_loss=False,
        pad_length_to_multiple_of: int | None = None,
    ):
        def combine_keys(key):
            return [item[key] for item in batch]

        lengths = combine_keys("lengths")
        rewards = combine_keys("reward")
        seq_boundaries = combine_keys("seq_boundaries")

        input_ids = [
            np.concatenate(
                [
                    item["input_ids"][item["seq_boundaries"][i] : item["seq_boundaries"][i + 1] - 1]
                    for i in range(len(item["seq_boundaries"]) - 1)
                ]
            )
            for item in batch
        ]
        labels = [
            np.concatenate(
                [
                    item["labels"][item["seq_boundaries"][i] + 1 : item["seq_boundaries"][i + 1]]
                    for i in range(len(item["seq_boundaries"]) - 1)
                ]
            )
            for item in batch
        ]

        if pad_length_to_multiple_of:
            max_seq_len = torch.tensor(max(ex.shape[0] for ex in input_ids), device=torch.cuda.current_device())
            torch.distributed.all_reduce(
                max_seq_len, op=torch.distributed.ReduceOp.MAX, group=parallel_state.get_data_parallel_group()
            )
            max_length = math.ceil(max_seq_len / pad_length_to_multiple_of) * pad_length_to_multiple_of
        else:
            # pad to the nearest multiple of 16 for FP8 training
            # for many datasets in practice, all packed sequence lengths are very close to the
            # target length (2048, 4096, 8192), so there is very minimal padding
            max_length = max(len(l) for l in input_ids)
            max_length = min(self.seq_length, self._ceil_to_nearest(max_length, 16))

        position_ids: List[List[int]] = []
        cu_seqlens: List[List[int]] = []
        for item in batch:
            position_ids.append([])
            cu_seqlens.append([0])
            seqlens = np.array(item["seq_boundaries"][1:]) - np.array(item["seq_boundaries"][:-1])
            for l in seqlens:
                position_ids[-1].extend(list(range(l - 1)))  ## l - 1 to exclude labels
                cu_seqlens[-1].append(cu_seqlens[-1][-1] + l - 1)
            # set last seq to the max seq len because rope and attn kernels expect no padding
            cu_seqlens[-1][-1] = max_length

        assert len(input_ids[0]) == len(
            position_ids[0]
        ), "Dataset problem: input_ids and position_ids lengths don't match"

        input_ids = self._collate_item(input_ids, max_length=max_length, pad_id=self.tokenizer.eos_id)
        labels = self._collate_item(labels, max_length=max_length, pad_id=self.LABELS_PAD_ID)
        position_ids = self._collate_item(position_ids, max_length=max_length, pad_id=0)

        max_num_sequences = max(len(l) for l in lengths)
        lengths = self._collate_item(lengths, max_length=max_num_sequences, pad_id=0)
        rewards = self._collate_item(rewards, max_length=max_num_sequences, pad_id=self.REWARDS_PAD_ID)

        output = {
            "input_ids": torch.LongTensor(input_ids),
            "labels": torch.LongTensor(labels),
            "lengths": torch.LongTensor(lengths),
            "rewards": torch.FloatTensor(rewards),
            "position_ids": torch.LongTensor(position_ids),
        }

        cu_seqlens = self._collate_item(cu_seqlens, max_length=max(len(l) for l in cu_seqlens) + 1, pad_id=-1)

        # Pre-generate `cu_seqlens_argmin` and `max_seqlen` as CPU tensor to avoid device-to-host copies.
        cu_seqlens = torch.IntTensor(cu_seqlens)
        cu_seqlens_argmin = torch.argmin(cu_seqlens, dim=1, keepdim=True)
        seqlens = cu_seqlens[:, 1:] - cu_seqlens[:, :-1]
        max_seqlen, _ = seqlens.max(dim=1, keepdim=True)

        output.update(
            {
                "attention_mask": torch.LongTensor(
                    [1] * len(input_ids)
                ),  # no attention mask is needed for packed seq, this serves as a placeholder
                "cu_seqlens": torch.IntTensor(cu_seqlens),  # cu_seqlens_q must be in dtype torch.int32
                "cu_seqlens_argmin": cu_seqlens_argmin,  # only required for perf
                "max_seqlen": max_seqlen,  # only required for perf
            }
        )

        return output


class KTOModelDataset(Dataset):
    """This class works only with jsonl files. It assumes each line of the json file is a dictionary
    with the prompt, along with the response (response only, no prompt), and the status denoting whether the response is
    chosen or rejected. This Dataset will combine the prompt with the corresponding response, and then tokenize it. It
    will also create a score field that has 1 if the sample is chosen and 0 if rejected. It also returns the labels for
    each, which is the response tokens with -100 for the prompt part.

    WARNING: This class will tokenize the text, but it will raise an exception on model max seq len violations!
             Meaning it will not truncate tokens to fit to model max seq len, because of special prefix/suffix
             strings such as <extra_id_1>, it would not know where it is safe to truncate for each model. Therefore,
             the user must do all truncation logic in their preprocessing step when generating the jsonl
             used by this class. Put all special truncation logic there specific to your model.
    """

    def __init__(
        self, cfg, tokenizer, name, data_prefix, documents, data, seq_length, seed, drop_last=True,
    ):
        super().__init__()
        self.cfg = cfg
        self.name = name
        self.data = data
        self.drop_last = drop_last
        self.seq_length = seq_length
        self.tokenizer = tokenizer

        self.reset_position_ids = cfg.data.get("reset_position_ids", False)
        self.reset_attention_mask = cfg.data.get("reset_attention_mask", False)
        self.eod_mask_loss = cfg.data.get("eod_mask_loss", False)
        self.eos_id = tokenizer.eos_id

        np_rng = np.random.default_rng(seed=seed)
        np_rng.shuffle(self.data)

        self.nograd_length = 32

        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < len(self.data)

    def __len__(self):
        return len(self.data)

    def encode(self, text, append_eod=False, add_dummy_prefix=True):
        if self.cfg.data.get("apply_ftfy", False):
            import ftfy

            text = ftfy.fix_text(text)

        text_ids = self.tokenizer.text_to_ids(text)

        if len(text_ids) > 0 and append_eod:
            text_ids.append(self.tokenizer.eos_id)

        return text_ids, len(text_ids)

    def __getitem__(self, idx):
        """Returns a sample = prompt + response, their respective lengths, and labels.
        Differently from DPO, we need to separate the prompt from the response.
        """
        payload = self.data[idx]
        prompt, prompt_len = self.encode(payload["prompt"], append_eod=False)
        sample, sample_len = self.encode(
            payload["prompt"] + payload["response"], append_eod=self.cfg.data.get("append_eod", False)
        )
        labels = ([-100] * prompt_len) + sample[prompt_len:]
        # Separate the response from the prompt
        response = sample[prompt_len:]
        preference = 1 if payload["preference"] == "chosen" else 0

        assert sample[0:prompt_len] == prompt, "the tokenizer for KTO has merged tokens between prompt and response"

        if sample_len > self.seq_length:
            logging.warning(
                f"WARNING: Tokenized text exceeds max seq length ({sample_len} vs {self.seq_length})."
                + f"The example will be ignored."
            )
            # Truncate the sample and labels to the first nograd_length tokens
            sample_len = self.nograd_length
            sample = sample[: self.nograd_length]
            prompt_len = self.nograd_length // 2
            prompt = prompt[:prompt_len]
            response = sample[prompt_len:]
            labels = torch.ones_like(torch.LongTensor(sample)) * (-100)

        output = {
            "prompt_tokens": torch.LongTensor(prompt),
            "response_tokens": torch.LongTensor(response),
            "sample_length": sample_len,
            "sample_labels": torch.LongTensor(labels),
            "preference": preference,
        }
        return output


class RegressionRewardModelDataset(RewardModelDataset):
    """This class assumes each line of the dataset file is a dictionary with "text" and "label" field,
    where "text" is a string representing the input prompt, and "label" is a list of float or int values.
    Note that when training the model with multiple datasets which contain different attributes,
    we should set missing attributes to model.regression.loss_mask_val(according to training_rm.yaml)
    in the dataset files so that their losses are masked. At least one attribute should be present for each sample.

    WARNING: It's recommended to preprocess your data in advance to ensure all samples are within self.seq_length.
             Otherwise if all samples in a batch are longer than self.seq_length, you may get NaN loss.
    """

    def __init__(
        self, cfg, tokenizer, name, data_prefix, documents, data, seq_length, seed, drop_last=True,
    ):

        assert cfg.data.data_impl.startswith(
            "json"
        ), f"data.data_impl must be either json or jsonl, but got {cfg.data.data_impl}"

        super().__init__(
            cfg=cfg,
            tokenizer=tokenizer,
            name=name,
            data_prefix=data_prefix,
            documents=documents,
            data=data,
            seq_length=seq_length,
            seed=seed,
            drop_last=drop_last,
        )

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        """
        Returns one training sample, its label, and its respective length.
        """

        orig_idx = idx = idx % len(self)
        while True:
            sample = self.data[idx]
            sample_text, sample_length = self.encode(sample["text"])
            sample_label = sample["label"]
            if idx == orig_idx:
                orig_length = sample_length
            if sample_length <= self.seq_length:
                break

            idx = (idx + 1) % len(self)
            if idx == orig_idx:
                raise RuntimeError(f"All samples have length > {self.seq_length}")

        assert isinstance(sample_label, list) and all(
            isinstance(value, (float, int)) for value in sample_label
        ), "label should be a list of float or int values"

        sample_label = [float(value) for value in sample_label]

        label_tensor = torch.tensor(sample_label, dtype=torch.float)

        text_np = np.array(sample_text, dtype=np.int64)
        text_np_pad = np.pad(
            text_np, (0, max(0, self.seq_length - text_np.shape[0])), mode="constant", constant_values=self.eos_id
        )

        text_tensor = torch.tensor(text_np_pad)
        attention_mask, loss_mask, position_ids = _create_ltor_masks_and_position_ids(
            text_tensor, self.eos_id, self.reset_position_ids, self.reset_attention_mask, self.eod_mask_loss,
        )

        # Negative index comes when we pad the last batch in MegatronPretrainingBatchSampler
        # We make the loss_mask zero to mask out loss from these samples
        if idx == -1:
            logging.waring("WARNING: Got -1 as item index. Masking loss from this sample")
            loss_mask = torch.zeros_like(loss_mask)

        # Replace current sample (when it exceeds max length) with another sample but mask its loss
        if idx != orig_idx:
            logging.warning(
                f"Sample {orig_idx} in dataset '{self.name}' has length "
                f"{orig_length} > {self.seq_length} "
                f"=> replacing it with sample {idx} and masking its loss"
            )
            loss_mask = torch.zeros_like(loss_mask)

        output = {
            "inputs": text_tensor,
            "lengths": text_np.shape[0],
            "position_ids": position_ids,
            "attention_mask": attention_mask,
            "loss_mask": loss_mask,
            "labels": label_tensor,
        }
        return output


class SteerLM2Dataset(GPTSFTChatDataset):
    def get_prompt(self, system_turn, prompt_turns):
        prompt = f"{self.special_tokens['system_turn_start']}System{self.special_tokens['end_of_name']}"
        prompt += f"{system_turn}{self.special_tokens['end_of_turn']}"
        for turn in prompt_turns:
            prompt += f"{self.special_tokens['turn_start']}{turn['from']}{self.special_tokens['end_of_name']}"
            prompt += f"{turn['value']}{self.special_tokens['end_of_turn']}"
        return prompt

    def _process_example(self, example):
        """
        Create an example by concatenating text and answer.
        Truncation is carried out when needed, but it is performed only on the prompt side.
        BOS, EOS, and SEP, are added if specified.
        """
        assert len(example["prompt_turns"]) % 2 == 1, "Number of prompt turns should be odd"
        prompt = self.get_prompt(example["system"], example["prompt_turns"])
        batched_token_ids = []
        batched_masks = []
        response_from = example["responses"][0]["from"]
        assert [item["from"] for item in example["responses"]] == [response_from] * len(
            example["responses"]
        ), "All responses should be from the same person"
        prompt += f"{self.special_tokens['turn_start']}{response_from}{self.special_tokens['end_of_name']}"
        if "label" in example and example["label"] is not None:
            prompt += f"{self.special_tokens['label_start']}{example['label']}{self.special_tokens['end_of_turn']}"
        prompt_tokens = self.tokenizer.text_to_ids(prompt)
        num_prompt_tokens = len(prompt_tokens)
        batch_size = len(example["responses"])
        logws = []
        logqs = []
        for item in example["responses"]:
            full_text = prompt
            full_text += item["value"] + self.special_tokens["end_of_turn"] + self.special_tokens["turn_start"]
            token_ids = self.tokenizer.text_to_ids(full_text)
            masks = [0] * num_prompt_tokens + [1] * (len(token_ids) - num_prompt_tokens)
            logqs.append(item["log(Q(y|a,x))"])
            logw = item["log(P(a|x,y))"] + item["log(P(y|x))"] - item["log(Q(y|a,x))"]
            logws.append(logw)
            batched_token_ids.append(token_ids)
            batched_masks.append(masks)
        logws = np.array(logws)
        ws = scipy.special.softmax(logws)
        processed_batch = {
            "input_ids": batched_token_ids,
            "mask": batched_masks,
            "ws": ws,
            "log(Q(y|a,x))": logqs,
        }
        return processed_batch

    def collate_fn(self, batch):
        # return batch
        input_ids = [item[:-1] for one_batch in batch for item in one_batch["input_ids"]]
        labels = [item[1:] for one_batch in batch for item in one_batch["input_ids"]]
        loss_mask = [item[1:] for one_batch in batch for item in one_batch["mask"]]
        ws = [item.item() for one_batch in batch for item in one_batch["ws"]]
        logqs = [item for one_batch in batch for item in one_batch["log(Q(y|a,x))"]]
        num_responses = [len(one_batch["input_ids"]) for one_batch in batch for item in one_batch["input_ids"]]
        # assert num_responses all have the same number and only one number
        assert len(set(num_responses)) == 1
        max_length = max([len(x) for x in input_ids])

        if max_length > self.max_seq_length:
            # truncate the sequences if it is longer than max_seq_length
            input_ids = [x[: self.max_seq_length] for x in input_ids]
            labels = [x[: self.max_seq_length] for x in labels]
            loss_mask = [x[: self.max_seq_length] for x in loss_mask]

        # increase max length to nearest multiple of 4 or 8
        if self.pad_to_max_length:
            max_length = self.max_seq_length
        else:
            max_length = min(self.max_seq_length, self._ceil_to_nearest(max_length, 8))
        assert max_length <= self.max_seq_length

        attention_mask = [self._create_attention_mask(max_length) for _ in batch]
        attention_mask = torch.stack(attention_mask)
        position_ids = [list(range(max_length)) for _ in batch]
        position_ids = torch.LongTensor(position_ids)
        input_ids = torch.LongTensor(
            self._collate_item(input_ids, max_length=max_length, pad_id=self.tokenizer.eos_id)
        )
        labels = torch.LongTensor(self._collate_item(labels, max_length=max_length, pad_id=self.tokenizer.eos_id))
        loss_mask = torch.LongTensor(self._collate_item(loss_mask, max_length=max_length, pad_id=0))
        ws = torch.FloatTensor(ws)
        logqs = torch.FloatTensor(logqs)
        num_responses = torch.LongTensor(num_responses)

        processed_batch = {
            "tokens": input_ids,
            "labels": labels,
            "attention_mask": attention_mask,
            "loss_mask": loss_mask,
            "position_ids": position_ids,
            "ws": ws,
            "log(Q(y|a,x))": logqs,
            "num_responses": num_responses,
        }

        return processed_batch
