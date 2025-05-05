# Copyright 2024 Bytedance Ltd. and/or its affiliates
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
"""
FSDP PPO Trainer with Ray-based single controller.
This trainer supports model-agonistic model initialization with huggingface
"""

import json
import os
import uuid
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import copy
import numpy as np
import random
import ray
import torch
from codetiming import Timer
from ray.experimental.tqdm_ray import tqdm
from torch.utils.data import RandomSampler, SequentialSampler
from torchdata.stateful_dataloader import StatefulDataLoader
from transformers import PreTrainedTokenizer, ProcessorMixin
from collections import defaultdict

from ..protocol import DataProto, pad_dataproto_to_divisor, unpad_dataproto
from ..single_controller.base import Worker
from ..single_controller.ray import RayClassWithInitArgs, RayResourcePool, RayWorkerGroup
from ..single_controller.ray.base import create_colocated_worker_cls
from ..utils import torch_functional as VF
from ..utils.checkpoint import CHECKPOINT_TRACKER, remove_obsolete_ckpt
from ..utils.dataset import collate_fn as collate_fn_raw
from ..utils.osworld import OSWorldDataset, OSWorldTaskConfigDataset, OSWorldGRPODataset, collate_fn, collate_fn_dataproto, collate_fn_fake
from ..utils.logger import Tracker
from ..utils.py_functional import convert_dict_to_str
from ..utils.seqlen_balancing import get_seqlen_balanced_partitions, log_seqlen_unbalance
from ..workers.fsdp_workers import FSDPWorker
from . import core_algos
from .config import PPOConfig
from .metrics import compute_data_metrics, compute_throughout_metrics, compute_timing_metrics, reduce_metrics

from .gui_agent import EnvWorker

from collections import defaultdict
from qwen_vl_utils import process_vision_info

from concurrent.futures import ThreadPoolExecutor


class Role(IntEnum):
    """
    To create more roles dynamically, you can subclass Role and add new members
    """

    Actor = auto()
    Rollout = auto()
    ActorRollout = auto()
    Critic = auto()
    RefPolicy = auto()
    RewardModel = auto()
    ActorRolloutRef = auto()


class AdvantageEstimator(str, Enum):
    """
    Using an enumeration class to avoid spelling errors in adv_estimator
    """

    GAE = "gae"
    GRPO = "grpo"
    REINFORCE_PLUS_PLUS = "reinforce_plus_plus"
    REMAX = "remax"
    RLOO = "rloo"


@dataclass
class ResourcePoolManager:
    """
    Define a resource pool specification. Resource pool will be initialized first.
    """

    resource_pool_spec: dict[str, list[int]]
    mapping: dict[Role, str]
    resource_pool_dict: dict[str, RayResourcePool] = field(default_factory=dict)

    def create_resource_pool(self):
        for resource_pool_name, process_on_nodes in self.resource_pool_spec.items():
            # max_colocate_count means the number of WorkerGroups (i.e. processes) in each RayResourcePool
            # For FSDP backend, we recommend using max_colocate_count=1 that merge all WorkerGroups into one.
            # For Megatron backend, we recommend using max_colocate_count>1 that can utilize different WorkerGroup for differnt models
            resource_pool = RayResourcePool(
                process_on_nodes=process_on_nodes, use_gpu=True, max_colocate_count=1, name_prefix=resource_pool_name
            )
            self.resource_pool_dict[resource_pool_name] = resource_pool

        self._check_resource_available()

    def get_resource_pool(self, role: Role) -> RayResourcePool:
        """Get the resource pool of the worker."""
        return self.resource_pool_dict[self.mapping[role]]

    def get_n_gpus(self) -> int:
        """Get the number of gpus in this cluster."""
        return sum([n_gpus for process_on_nodes in self.resource_pool_spec.values() for n_gpus in process_on_nodes])

    def _check_resource_available(self):
        """Check if the resource pool can be satisfied in this ray cluster."""
        node_available_resources = ray.state.available_resources_per_node()
        node_available_gpus = {node: node_info.get("GPU", 0) for node, node_info in node_available_resources.items()}

        # check total required gpus can be satisfied
        total_available_gpus = sum(node_available_gpus.values())
        total_required_gpus = sum(
            [n_gpus for process_on_nodes in self.resource_pool_spec.values() for n_gpus in process_on_nodes]
        )
        if total_available_gpus < total_required_gpus:
            raise ValueError(
                f"Total available GPUs {total_available_gpus} is less than total desired GPUs {total_required_gpus}."
            )


def apply_kl_penalty(data: DataProto, kl_ctrl: core_algos.KLController, kl_penalty="kl"):
    token_level_scores = data.batch["token_level_scores"]
    batch_size = data.batch.batch_size[0]
    response_mask = data.batch["response_mask"]

    # compute kl between ref_policy and current policy
    if "ref_log_probs" in data.batch.keys():
        kld = core_algos.compute_kl(data.batch["old_log_probs"], data.batch["ref_log_probs"], kl_penalty=kl_penalty)
        kld = kld * response_mask  # (batch_size, response_length)
    else:
        kld = torch.zeros_like(response_mask, dtype=torch.float32)

    data.batch["token_level_rewards"] = token_level_scores - kl_ctrl.kl_coef * kld

    current_kl = VF.masked_mean(kld, mask=response_mask, dim=-1)  # average over sequence
    current_kl = torch.mean(current_kl, dim=0).item()
    metrics = {"critic/kl": current_kl, "critic/kl_coef": kl_ctrl.kl_coef}

    # According to https://github.com/huggingface/trl/blob/v0.11.0/trl/trainer/ppo_trainer.py#L880
    kl_ctrl.update(current_kl=current_kl, n_steps=batch_size)
    return data, metrics


def compute_advantage(data: DataProto, adv_estimator: AdvantageEstimator, gamma: float = 1.0, lam: float = 1.0):
    token_level_rewards = data.batch["token_level_rewards"]
    response_mask = data.batch["response_mask"]
    index = data.non_tensor_batch["uid"]
    if adv_estimator == AdvantageEstimator.GAE:
        values = data.batch["values"]
        advantages, returns = core_algos.compute_gae_advantage_return(
            token_level_rewards, values, response_mask, gamma, lam
        )
    elif adv_estimator == AdvantageEstimator.GRPO:
        advantages, returns = core_algos.compute_grpo_outcome_advantage(token_level_rewards, response_mask, index)
    elif adv_estimator == AdvantageEstimator.REINFORCE_PLUS_PLUS:
        advantages, returns = core_algos.compute_reinforce_plus_plus_outcome_advantage(
            token_level_rewards, response_mask, gamma
        )
    elif adv_estimator == AdvantageEstimator.REMAX:
        reward_baselines = data.batch["reward_baselines"]
        advantages, returns = core_algos.compute_remax_outcome_advantage(
            token_level_rewards, reward_baselines, response_mask
        )
    elif adv_estimator == AdvantageEstimator.RLOO:
        advantages, returns = core_algos.compute_rloo_outcome_advantage(token_level_rewards, response_mask, index)
    else:
        raise NotImplementedError

    data.batch["advantages"] = advantages
    data.batch["returns"] = returns
    return data


@contextmanager
def _timer(name: str, timing_raw: Dict[str, float]):
    with Timer(name=name, logger=None) as timer:
        yield

    timing_raw[name] = timer.last


class RayPPOTrainer:
    """
    Note that this trainer runs on the driver process on a single CPU/GPU node.
    """

    def __init__(
        self,
        config: PPOConfig,
        tokenizer: PreTrainedTokenizer,
        processor: Optional[ProcessorMixin],
        role_worker_mapping: dict[Role, Type[Worker]],
        resource_pool_manager: ResourcePoolManager,
        ray_worker_group_cls: Type[RayWorkerGroup] = RayWorkerGroup,
        reward_fn: Optional[Callable[[DataProto], Tuple[torch.Tensor, Dict[str, List[float]]]]] = None,
        val_reward_fn: Optional[Callable[[DataProto], Tuple[torch.Tensor, Dict[str, List[float]]]]] = None,
    ):
        self.tokenizer = tokenizer
        self.processor = processor
        self.config = config
        self.reward_fn = reward_fn
        self.val_reward_fn = val_reward_fn

        self.hybrid_engine = config.worker.hybrid_engine
        if self.hybrid_engine:
            assert Role.ActorRollout in role_worker_mapping, (
                f"ActorRollout should be included in {role_worker_mapping.keys()}."
            )
        else:
            raise NotImplementedError

        self.role_worker_mapping = role_worker_mapping
        self.resource_pool_manager = resource_pool_manager
        self.use_reward_model = Role.RewardModel in role_worker_mapping
        self.ray_worker_group_cls = ray_worker_group_cls

        # define KL control
        if Role.RefPolicy in role_worker_mapping and not config.algorithm.disable_kl:
            self.use_reference_policy = True
            self.kl_ctrl = core_algos.get_kl_controller(config.algorithm)
        else:
            self.use_reference_policy = False
            self.kl_ctrl = core_algos.FixedKLController(init_kl_coef=0.0)
            print("KL is disabled, no KL metrics will be logged. Please set `kl_coef=0` to log KL metrics.")
        
        if config.algorithm.adv_estimator == AdvantageEstimator.GAE:
            self.use_critic = True
        else:
            self.use_critic = False

        if config.algorithm.adv_estimator not in list(AdvantageEstimator):
            raise NotImplementedError(f"Unknown advantage estimator: {config.algorithm.adv_estimator}.")

        if config.data.rollout_batch_size % config.worker.actor.global_batch_size != 0:
            raise ValueError("Rollout batch size must be divisible by actor global batch size.")

        if (
            config.data.rollout_batch_size * config.worker.rollout.n
        ) % config.worker.actor.micro_batch_size_per_device_for_experience != 0:
            raise ValueError(
                "Rollout batch size * rollout.n must be divisible by actor micro batch size for experience."
            )

        if self.use_critic:
            if config.data.rollout_batch_size % config.worker.critic.global_batch_size != 0:
                raise ValueError("Rollout batch size must be divisible by critic global batch size.")

            if (
                config.data.rollout_batch_size * config.worker.rollout.n
            ) % config.worker.critic.micro_batch_size_per_device_for_experience != 0:
                raise ValueError(
                    "Rollout batch size * rollout.n must be divisible by critic micro batch size for experience."
                )

        if (
            config.algorithm.adv_estimator in (AdvantageEstimator.GRPO, AdvantageEstimator.RLOO)
            and config.worker.rollout.n == 1
        ):
            raise ValueError("GRPO and RLOO algorithm need `config.worker.rollout.n > 1`.")
        
        print(config)

        self.fake_dataset = None
        self._create_dataloader()
        self._create_envs()


    def _create_envs(self) -> None:
        print('Start to create env_worker for OSWorld Environment')
        max_steps = self.config.env.max_steps
        self.env_workers = [EnvWorker.remote(i, max_steps) for i in range(self.config.env.num_envs)]
        print('Env_worker for OSWorld Environment created!')
            
            
    def _create_dataloader(self) -> None:
        self.train_dataset = OSWorldTaskConfigDataset(
            data_path=self.config.data.train_files,
        )
        # data = self.train_dataset[0]
        # breakpoint()
        # use sampler for better ckpt resume
        if self.config.data.shuffle:
            train_dataloader_generator = torch.Generator()
            train_dataloader_generator.manual_seed(self.config.data.seed)
            sampler = RandomSampler(data_source=self.train_dataset, generator=train_dataloader_generator)
        else:
            sampler = SequentialSampler(data_source=self.train_dataset)

        self.train_dataloader = StatefulDataLoader(
            dataset=self.train_dataset,
            batch_size=self.config.data.rollout_batch_size,
            sampler=sampler,
            num_workers=8,
            collate_fn=collate_fn,
            pin_memory=False,
            drop_last=True,
        )

        self.val_dataset = OSWorldTaskConfigDataset(
            data_path=self.config.data.val_files,
        )
        
        self.val_dataloader = StatefulDataLoader(
            dataset=self.val_dataset,
            batch_size=min(self.config.env.num_envs, len(self.val_dataset)), # use the same number as envs
            shuffle=False,
            num_workers=8,
            collate_fn=collate_fn,
            pin_memory=False,
            drop_last=False,
        )

        assert len(self.train_dataloader) >= 1
        assert len(self.val_dataloader) >= 1
        print(f"Size of train dataloader: {len(self.train_dataloader)}")
        print(f"Size of val dataloader: {len(self.val_dataloader)}")

        if self.config.trainer.max_steps is not None:
            training_steps = self.config.trainer.max_steps
        else:
            training_steps = len(self.train_dataloader) * self.config.trainer.total_episodes

        self.training_steps = training_steps
        self.config.worker.actor.optim.training_steps = training_steps
        self.config.worker.critic.optim.training_steps = training_steps
        print(f"Total training steps: {self.training_steps}")

    def _maybe_log_val_generations(
        self, inputs: List[str], outputs: List[str], labels: List[str], scores: List[float]
    ) -> None:
        """Log a table of validation samples"""
        if self.config.trainer.val_generations_to_log <= 0:
            return

        # Create tuples of (input, output, score) and sort by input text
        samples = list(zip(inputs, outputs, labels, scores))
        samples.sort(key=lambda x: x[0])  # Sort by input text

        # Use fixed random seed for deterministic shuffling
        rng = np.random.RandomState(42)
        rng.shuffle(samples)

        samples = samples[: self.config.trainer.val_generations_to_log]
        self.logger.log_generation(samples, self.global_step)

    def _validate(self) -> Dict[str, Any]:
        reward_tensor_lst = []
        # Lists to collect samples for the table
        sample_inputs, sample_outputs, sample_labels, sample_scores = [], [], [], []
        reward_metrics_lst = defaultdict(list)
        for batch_dict in self.val_dataloader:
            task_configs = batch_dict

            reset_outputs = ray.get([
                worker.reset.remote(task_config) for worker, task_config in
                zip(self.env_workers, task_configs)
            ])

            self.actor_rollout_wg.prepare_generate_sequences()

            env_outputs = reset_outputs
            for step_idx in range(self.config.env.max_steps):
                print(f"Step {step_idx} of {self.config.env.max_steps}: {ray.get([worker.is_done.remote() for worker in self.env_workers])}")
                num_workers = len(self.env_workers)

                vllm_batch, valid_env_idx = self.prepare_vllm_inputs_full(env_outputs)

                vllm_batch_pad, pad_size = pad_dataproto_to_divisor(vllm_batch, num_workers)
                

                gen_batch = vllm_batch_pad.pop(
                    batch_keys=["input_ids", "attention_mask", "position_ids"],
                    non_tensor_batch_keys=["raw_prompt_ids", "multi_modal_data", "multi_modal_inputs"],
                )

                # override val config
                gen_batch.meta_info = self.config.worker.rollout.val_override_config

                # predict actions
                action_batch_output = self.actor_rollout_wg.generate_sequences(gen_batch)
                action_batch_output = unpad_dataproto(action_batch_output, pad_size=pad_size)
                
                response_texts = self.tokenizer.batch_decode(action_batch_output.batch['responses'], skip_special_tokens=True)

                cur_valid_envs = [self.env_workers[i] for i in valid_env_idx]

                env_outputs = ray.get([
                    worker.step.remote(action_text) for worker, action_text in zip(cur_valid_envs, response_texts)
                ])

                is_all_done = all([x['is_done'] for x in env_outputs])
                if is_all_done:
                    break

            eval_results = ray.get([worker.evaluate.remote() for worker in self.env_workers])
            history_messages = ray.get([worker.get_history_messages.remote() for worker in self.env_workers])
            self.actor_rollout_wg.finish_generate_sequences()

            # Store scores
            scores = eval_results
            reward_tensor = torch.tensor(scores, dtype=torch.float32).unsqueeze(-1)

            sample_inputs.extend([task_config['instruction'] for task_config in task_configs])
            prompts = []
            for history_message in history_messages:
                prompts.append(self.processor.apply_chat_template(history_message))
            
            sample_outputs.extend(prompts)
            sample_labels.extend(['none']*len(prompts))
            sample_scores.extend(scores)

            reward_tensor_lst.append(reward_tensor)

        self._maybe_log_val_generations(sample_inputs, sample_outputs, sample_labels, sample_scores)
        reward_score = torch.cat(reward_tensor_lst, dim=0).sum(-1).mean().item()
        return {"val/reward_score": reward_score}

    def init_workers(self) -> None:
        """Init resource pool and worker group"""
        self.resource_pool_manager.create_resource_pool()
        self.resource_pool_to_cls = {pool: {} for pool in self.resource_pool_manager.resource_pool_dict.values()}

        # create actor and rollout
        if self.hybrid_engine:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.ActorRollout)
            actor_rollout_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[Role.ActorRollout], config=self.config.worker, role="actor_rollout"
            )
            self.resource_pool_to_cls[resource_pool]["actor_rollout"] = actor_rollout_cls
        else:
            raise NotImplementedError

        # create critic
        if self.use_critic:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.Critic)
            critic_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[Role.Critic], config=self.config.worker, role="critic"
            )
            self.resource_pool_to_cls[resource_pool]["critic"] = critic_cls

        # create reference policy if needed
        if self.use_reference_policy:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.RefPolicy)
            ref_policy_cls = RayClassWithInitArgs(
                self.role_worker_mapping[Role.RefPolicy], config=self.config.worker, role="ref"
            )
            self.resource_pool_to_cls[resource_pool]["ref"] = ref_policy_cls

        # create a reward model if reward_fn is None
        if self.use_reward_model:
            # we create a RM here
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.RewardModel)
            rm_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[Role.RewardModel], config=self.config.worker, role="reward"
            )
            self.resource_pool_to_cls[resource_pool]["rm"] = rm_cls

        # initialize WorkerGroup
        # NOTE: if you want to use a different resource pool for each role, which can support different parallel size,
        # you should not use `create_colocated_worker_cls`. Instead, directly pass different resource pool to different worker groups.
        # See https://github.com/volcengine/verl/blob/master/examples/ray/tutorial.ipynb for more information.
        all_wg: Dict[str, FSDPWorker] = {}
        self.wg_dicts = []
        for resource_pool, class_dict in self.resource_pool_to_cls.items():
            worker_dict_cls = create_colocated_worker_cls(class_dict=class_dict)
            wg_dict = self.ray_worker_group_cls(resource_pool=resource_pool, ray_cls_with_init=worker_dict_cls)
            spawn_wg = wg_dict.spawn(prefix_set=class_dict.keys())
            all_wg.update(spawn_wg)
            # keep the referece of WorkerDict to support ray >= 2.31. Ref: https://github.com/ray-project/ray/pull/45699
            self.wg_dicts.append(wg_dict)

        if self.use_critic:
            self.critic_wg = all_wg["critic"]
            self.critic_wg.init_model()

        if self.use_reference_policy:
            self.ref_policy_wg = all_wg["ref"]
            self.ref_policy_wg.init_model()

        if self.use_reward_model:
            self.rm_wg = all_wg["rm"]
            self.rm_wg.init_model()

        # we should create rollout at the end so that vllm can have a better estimation of kv cache memory
        self.actor_rollout_wg = all_wg["actor_rollout"]
        self.actor_rollout_wg.init_model()

    def _save_checkpoint(self) -> None:
        # path: {save_checkpoint_path}/global_step_{global_step}/{actor,critic}
        remove_obsolete_ckpt(
            self.config.trainer.save_checkpoint_path, self.global_step, self.config.trainer.save_limit
        )
        folder_path = os.path.join(self.config.trainer.save_checkpoint_path, f"global_step_{self.global_step}")
        actor_path = os.path.join(folder_path, "actor")
        self.actor_rollout_wg.save_checkpoint(actor_path)

        if self.use_critic:
            critic_path = os.path.join(folder_path, "critic")
            self.critic_wg.save_checkpoint(critic_path)

        dataloader_path = os.path.join(folder_path, "dataloader.pt")
        dataloader_state_dict = self.train_dataloader.state_dict()
        torch.save(dataloader_state_dict, dataloader_path)

        last_global_step_path = os.path.join(self.config.trainer.save_checkpoint_path, CHECKPOINT_TRACKER)
        with open(last_global_step_path, "w") as f:
            f.write(str(self.global_step))

    def _load_checkpoint(self) -> None:
        if self.config.trainer.load_checkpoint_path is None:
            return

        if "global_step_" not in self.config.trainer.load_checkpoint_path.strip(os.path.sep).split(os.path.sep)[-1]:
            raise ValueError("`load_checkpoint_path` should end with `global_step_*`.")

        print(f"Load from checkpoint: {self.config.trainer.load_checkpoint_path}.")
        self.global_step = int(self.config.trainer.load_checkpoint_path.strip(os.path.sep).split("global_step_")[-1])
        actor_path = os.path.join(self.config.trainer.load_checkpoint_path, "actor")
        self.actor_rollout_wg.load_checkpoint(actor_path)
        if self.use_critic:
            critic_path = os.path.join(self.config.trainer.load_checkpoint_path, "critic")
            self.critic_wg.load_checkpoint(critic_path)

        dataloader_path = os.path.join(self.config.trainer.load_checkpoint_path, "dataloader.pt")
        if os.path.exists(dataloader_path):
            dataloader_state_dict = torch.load(dataloader_path, weights_only=False)
            self.train_dataloader.load_state_dict(dataloader_state_dict)
        else:
            print(f"No dataloader state found at {dataloader_path}, will start from scratch.")

    def _balance_batch(self, batch: DataProto, metrics: Dict[str, Any], logging_prefix: str = "global_seqlen") -> None:
        """Reorder the data on single controller such that each dp rank gets similar total tokens"""
        attention_mask = batch.batch["attention_mask"]
        batch_size = attention_mask.shape[0]
        global_seqlen_lst = batch.batch["attention_mask"].view(batch_size, -1).sum(-1).tolist()  # (train_batch_size,)
        world_size = self.actor_rollout_wg.world_size
        global_partition_lst = get_seqlen_balanced_partitions(
            global_seqlen_lst, k_partitions=world_size, equal_size=True
        )
        # reorder based on index. The data will be automatically equally partitioned by dispatch function
        global_idx = torch.tensor([j for partition in global_partition_lst for j in partition])
        batch.reorder(global_idx)
        global_balance_stats = log_seqlen_unbalance(
            seqlen_list=global_seqlen_lst, partitions=global_partition_lst, prefix=logging_prefix
        )
        metrics.update(global_balance_stats)

    
    def fake_batch(self, batch_dict):
        batch_data = DataProto.from_single_dict(batch_dict)

        # set uid for each data
        uids = [x['task_id'] for x in batch_dict]
        batch_data.non_tensor_batch["uid"] = uid
        
        return batch_data
    
    def prepare_vllm_inputs_full(self, env_outputs: List):
        # NOTE: processor will be very slow
        obs_messages = [x['obs_messages'] for x in env_outputs]
        env_idx = [x['env_idx'] for x in env_outputs]

        valid_obs_messages = [x['obs_messages'] for x in env_outputs if x['obs_messages'] is not None]
        valid_env_idx = [x['env_idx'] for x in env_outputs if x['obs_messages'] is not None]

        dataset = OSWorldDataset(
            valid_obs_messages,
            tokenizer=self.tokenizer,
            processor=self.processor,
            max_prompt_length=self.config.data.max_prompt_length,
            truncation="right",
            format_prompt=self.config.data.format_prompt,
            max_pixels=self.config.data.max_pixels,
            min_pixels=self.config.data.min_pixels,
            fast_rollout=True,
        )

        # batch_dict = [dataset[i] for i in range(len(dataset))]
        def get_dataset_item(index):
            return dataset[index]

        with ThreadPoolExecutor(max_workers=32) as executor:
            batch_dict = list(executor.map(get_dataset_item, range(len(dataset))))

        # batch_dict = ray.get([get_dataset_item.remote(i) for i in range(len(dataset))])

        batch_dict = collate_fn_dataproto(batch_dict)
        batch = DataProto.from_single_dict(batch_dict)
        
        return batch, valid_env_idx


    def prepare_grpo_inputs(self, messages, eval_results, task_configs):
        eval_result_flatten = eval_results
        messages_flatten = messages

        dataset = OSWorldGRPODataset(
            messages_flatten,
            tokenizer=self.tokenizer,
            processor=self.processor,
            max_prompt_length=self.config.data.max_prompt_length,
            truncation="right",
            format_prompt=self.config.data.format_prompt,
            max_pixels=self.config.data.max_pixels,
            min_pixels=self.config.data.min_pixels,
        )
        def get_dataset_item(index):
            return dataset[index]

        with ThreadPoolExecutor(max_workers=32) as executor:
            batch_dict = list(executor.map(get_dataset_item, range(len(dataset))))
        # batch_dict = [get_dataset_item(i) for i in range(len(dataset))]
        
        batch_dict = collate_fn_dataproto(batch_dict)
        batch = DataProto.from_single_dict(batch_dict)

        # uid
        # use batch to compute norm reward
        batch.non_tensor_batch["uid"] = np.array([x['id'] for x in task_configs], dtype=object)
        batch.non_tensor_batch["task_id"] = np.array([x['id'] for x in task_configs], dtype=object)

        batch.batch["rewards"] = torch.tensor([float(x) for x in eval_result_flatten], dtype=torch.float32)

        return batch



            

    def save_rollout_trajectories(self, action_batch_output, history_messages, eval_results, task_configs):
        visual_trajs = dict()
        visual_trajs['history_messages'] = history_messages
        visual_trajs['eval_results'] = eval_results
        visual_trajs['task_configs'] = task_configs
    
        # os.makedirs(self.config.trainer.save_checkpoint_path, exist_ok=True)
        os.makedirs(os.path.join(self.config.trainer.save_checkpoint_path, "trajs"), exist_ok=True)
        visual_folder_path = os.path.join(self.config.trainer.save_checkpoint_path, "trajs", f"global_step_{self.global_step}.pth")
        torch.save(visual_trajs, visual_folder_path)
        action_batch_output.save_to_disk(os.path.join(self.config.trainer.save_checkpoint_path, "trajs", f"global_step_{self.global_step}_batch.pkl"))


    def fit(self):
        """
        The training loop of PPO.
        The driver process only need to call the compute functions of the worker group through RPC to construct the PPO dataflow.
        The light-weight advantage computation is done on the driver process.
        """
        self.logger = Tracker(loggers=self.config.trainer.logger, config=self.config.to_dict())
        self.global_step = 0
        val_metrics: Optional[Dict[str, Any]] = None

        # load checkpoint before doing anything
        self._load_checkpoint()

        # perform validation before training
        # currently, we only support validation using the reward_function.
        if self.config.trainer.val_before_train:
            val_metrics = self._validate()
            self.logger.log(data=val_metrics, step=self.global_step)
            if self.config.trainer.val_only:
                return

        rollout_n = self.config.worker.rollout.n
        for _ in tqdm(range(self.config.trainer.total_episodes), desc="Episode", position=0):
            iterator = iter(tqdm(self.train_dataloader, desc="Running step", position=1))

            for batch_dict in tqdm(self.train_dataloader, desc="Running step", position=1):

                task_configs = [x for x in batch_dict for _ in range(rollout_n)] # interleave

                self.global_step += 1
                if self.global_step > self.training_steps:
                    break

                metrics, timing_raw = {}, {}

                # batch: DataProto = DataProto.from_single_dict(batch_dict)
                # print(batch_dict)

                print(f"task_num: {len(task_configs)}, env_num: {len(self.env_workers)}")
                print([config['instruction'] for config in task_configs])

                with _timer("step", timing_raw):
                    self.actor_rollout_wg.prepare_generate_sequences()

                    history_messages_global = []
                    eval_results_global = []
                    assert len(task_configs) % len(self.env_workers) == 0, f"task_configs: {len(task_configs)}, env_num: {len(self.env_workers)}"

                    for rollout_idx in range(len(task_configs) // len(self.env_workers)):

                        cur_task_configs = task_configs[rollout_idx * len(self.env_workers):(rollout_idx + 1) * len(self.env_workers)]
                        # generate a batch
                        with _timer(f"gen_{rollout_idx}", timing_raw):  # wg: worker group

                            with _timer("env_reset", timing_raw):
                                reset_outputs = ray.get([
                                    worker.reset.remote(task_config) for worker, task_config in 
                                    zip(self.env_workers, cur_task_configs)
                                ])
                                # reset_outputs = ray.get(reset_outputs_ray_objects)
                                
                            print(f"rollout_n: {rollout_idx} | reset_time: {timing_raw['env_reset']}")

                            env_outputs = reset_outputs
                            for step_idx in range(self.config.env.max_steps):
                                print('step_idx: ', step_idx, ray.get([worker.is_done.remote() for worker in self.env_workers]))
                                num_workers = len(self.actor_rollout_wg._workers)

                                with _timer("prepare_vllm_inputs", timing_raw):
                                    vllm_batch, valid_env_idx = self.prepare_vllm_inputs_full(env_outputs)

                                print('prepare_vllm_inputs_time: ', timing_raw['prepare_vllm_inputs'])

                                vllm_batch_pad, pad_size = pad_dataproto_to_divisor(vllm_batch, num_workers)

                                gen_batch = vllm_batch_pad.pop(
                                    batch_keys=["input_ids", "attention_mask", "position_ids"],
                                    non_tensor_batch_keys=["raw_prompt_ids", "multi_modal_data", "multi_modal_inputs"],
                                )
                                # predict actions
                                with _timer("actor_rollout_wg", timing_raw):
                                    action_batch_output = self.actor_rollout_wg.generate_sequences(gen_batch)
                                print('action_batch_output_time: ', timing_raw['actor_rollout_wg'])
                                action_batch_output = unpad_dataproto(action_batch_output, pad_size=pad_size)

                                response_texts = self.tokenizer.batch_decode(action_batch_output.batch['responses'], skip_special_tokens=True)

                                cur_valid_envs = [self.env_workers[i] for i in valid_env_idx]
                                with _timer("env_step", timing_raw):
                                    env_outputs = ray.get([
                                        worker.step.remote(action_text) for worker, action_text in zip(cur_valid_envs, response_texts)
                                    ])
                                print('env_step_time: ', timing_raw['env_step'])

                                is_all_done = all([x['is_done'] for x in env_outputs])
                                if is_all_done:
                                    break
                                # response_texts = [text.replace('<|endoftext|>', '').replace('<|im_end|>', '') for text in response_texts] # <|box_start|>, <box_end|>
                            eval_results = ray.get([worker.evaluate.remote() for worker in self.env_workers])
                            history_messages = ray.get([worker.get_history_messages.remote() for worker in self.env_workers])
                            print(f'Rollout_n: {rollout_idx} |Evaluation results: {eval_results} | Mean acc: {np.mean(eval_results)}')

                            eval_results_global.extend(eval_results)
                            history_messages_global.extend(history_messages)

                        if self.global_step % 1 == 0:
                            self.save_rollout_trajectories(action_batch_output, history_messages, eval_results, task_configs)
                                
                    self.actor_rollout_wg.finish_generate_sequences()

                    batch = self.prepare_grpo_inputs(history_messages_global, eval_results_global, task_configs)
                    print('Global eval_results: ', sum(eval_results_global)/len(eval_results_global))

                    # batch.non_tensor_batch["uid"] = batch.non_tensor_batch["task_id"]
                    # add response_mask
                    batch.batch["responses"] = batch.batch["input_ids"]
                    batch.batch["response_mask"] = batch.batch["labels"]!=-100

                    # compute reward
                    with _timer("reward", timing_raw):
                        if self.use_reward_model:
                            raise NotImplementedError("Reward model is not supported yet.")

                        # we combine with rule-based rm
                        reward_tensor = batch.batch["rewards"]
                        reward_metrics = {"reward_tensor": reward_tensor.tolist()}

                        batch.batch["token_level_scores"] = reward_tensor.unsqueeze(-1)
                        reward_metrics = {
                            f"reward/{key}": value for key, value in reduce_metrics(reward_metrics).items()
                        }
                        metrics.update(reward_metrics)

                    # balance the number of valid tokens on each dp rank.
                    # Note that this breaks the order of data inside the batch.
                    # Please take care when you implement group based adv computation such as GRPO and rloo
                    self._balance_batch(batch, metrics=metrics)

                    # compute global_valid tokens
                    batch.meta_info["global_token_num"] = torch.sum(batch.batch["attention_mask"], dim=-1).tolist()

                    # recompute old_log_probs
                    with _timer("old", timing_raw):
                        old_log_probs = self.actor_rollout_wg.compute_log_probs(batch)
                        batch = batch.union(old_log_probs)

                    # compute ref_log_probs
                    if self.use_reference_policy:
                        with _timer("ref", timing_raw):
                            ref_log_probs = self.ref_policy_wg.compute_ref_log_probs(batch)
                            batch = batch.union(ref_log_probs)

                    # compute values
                    if self.use_critic:
                        with _timer("values", timing_raw):
                            values = self.critic_wg.compute_values(batch)
                            batch = batch.union(values)

                    with _timer("adv", timing_raw):
                        # apply kl penalty if available
                        if not self.config.algorithm.use_kl_loss and self.use_reference_policy:
                            # apply kl penalty to reward
                            batch, kl_metrics = apply_kl_penalty(
                                batch, kl_ctrl=self.kl_ctrl, kl_penalty=self.config.algorithm.kl_penalty
                            )
                            metrics.update(kl_metrics)
                        else:
                            batch.batch["token_level_rewards"] = batch.batch["token_level_scores"]

                        # compute advantages, executed on the driver process
                        batch = compute_advantage(
                            batch,
                            adv_estimator=self.config.algorithm.adv_estimator,
                            gamma=self.config.algorithm.gamma,
                            lam=self.config.algorithm.lam,
                        )

                    # update critic
                    if self.use_critic:
                        with _timer("update_critic", timing_raw):
                            critic_output = self.critic_wg.update_critic(batch)

                        critic_metrics = reduce_metrics(critic_output.non_tensor_batch)
                        metrics.update(critic_metrics)

                    # update actor
                    if self.config.trainer.critic_warmup <= self.global_step:
                        with _timer("update_actor", timing_raw):
                            actor_output = self.actor_rollout_wg.update_actor(batch)

                        actor_metrics = reduce_metrics(actor_output.non_tensor_batch)
                        metrics.update(actor_metrics)

                    # validate
                    if (
                        self.config.trainer.val_freq > 0
                        and self.global_step % self.config.trainer.val_freq == 0
                    ):
                        with _timer("validation", timing_raw):
                            val_metrics = self._validate()

                        metrics.update(val_metrics)

                    if self.config.trainer.save_freq > 0 and self.global_step % self.config.trainer.save_freq == 0:
                        with _timer("save_checkpoint", timing_raw):
                            self._save_checkpoint()

                # collect metrics
                n_gpus = self.resource_pool_manager.get_n_gpus()
                metrics.update(compute_data_metrics(batch=batch, use_critic=self.use_critic))
                metrics.update(compute_timing_metrics(batch=batch, timing_raw=timing_raw))
                metrics.update(compute_throughout_metrics(batch=batch, timing_raw=timing_raw, n_gpus=n_gpus))

                self.logger.log(data=metrics, step=self.global_step)

        # perform validation after training
        if (
            val_metrics is None
            or self.config.trainer.val_freq <= 0
            or self.global_step % self.config.trainer.val_freq != 0
        ):
            val_metrics = self._validate()
            self.logger.log(data=val_metrics, step=self.global_step)

        print(f"Final validation metrics: {convert_dict_to_str(val_metrics)}")

        if self.config.trainer.save_freq <= 0 or self.global_step % self.config.trainer.save_freq != 0:
            self._save_checkpoint()
