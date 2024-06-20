#!/usr/bin/env python
# coding: utf-8

# In[1]:


#create config
from dataclasses import dataclass

@dataclass
class Config: 
    target_shape = None # will transform t1n during preprocessing (computationally expensive)
    unet_img_shape = (160,256)
    channels = 1
    effective_train_batch_size=16
    train_batch_size = 1
    eval_batch_size = 1 
    num_dataloader_workers = 8
    num_epochs = 5000 # one epoch needs ~12min (x2 GPU), because their are more training samples due connected_components
    gradient_accumulation_steps = 1
    learning_rate = 1e-4
    lr_warmup_steps = 100 #500
    evaluate_epochs = 800
    deactivate2Devaluation = False
    deactivate3Devaluation = True
    evaluate_num_batches = 30 # one batch needs ~15s.  
    evaluate_num_batches_3d = 2
    evaluate_3D_epochs = 1000  # one 3D evaluation needs ~20min 
    mixed_precision = "fp16"  # `no` for float32, `fp16` for automatic mixed precision
    output_dir = "lesion-synthesis-3D-filling-cond-lesion"  # the model name locally and on the HF Hub
    dataset_train_path = "./datasets/synthesis/dataset_train/imgs"
    segm_train_path = "./datasets/synthesis/dataset_train/segm"
    masks_train_path = "./datasets/synthesis/dataset_train/masks"
    dataset_eval_path = "./datasets/synthesis/dataset_eval/imgs"
    segm_eval_path = "./datasets/synthesis/dataset_eval/segm"
    masks_eval_path = "./datasets/synthesis/dataset_eval/masks" 
    train_connected_masks=True
    eval_connected_masks=False
    num_inference_steps=50
    log_csv = False
    #add_lesion_technique = "other_lesions_99Quantile" # 'mean_intensity', 'other_lesions_1stQuantile', 'other_lesions_mean', 'other_lesions_median', 'other_lesions_3rdQuantile', 'other_lesions_99Quantile', 'empty' 
    #intermediate_timestep = 3 # starting from this timesteps. num_inference_steps means the whole pipeline and 1 the last step. 
    mode = "train" # 'train', 'eval' or "tuning_parameters"
    debug = False
    brightness_augmentation = True
    restrict_mask_to_wm=False
    proportion_training_circular_masks = 0
    eval_loss_timesteps=[20,80,140,200,260,320,380,440,560,620,680,740,800,860,920,980]
    restrict_train_slices = "mask"
    restrict_eval_slices = "mask"
    use_min_snr_loss=False
    snr_gamma=5

    push_to_hub = False  # whether to upload the saved model to the HF Hub
    #hub_model_id = "<your-username>/<my-awesome-model>"  # the name of the repository to create on the HF Hub
    #hub_private_repo = False
    #overwrite_output_dir = True  # overwrite the old model when re-running the notebook
    seed = 0
config = Config()


# In[2]:


#setup huggingface accelerate
import torch
import numpy as np
import accelerate
accelerate.commands.config.default.write_basic_config(config.mixed_precision)
#if there are problems with ports then add manually "main_process_port: 0" or another number to yaml file


# In[3]:


from pathlib import Path
import json
with open(Path.home() / ".cache/huggingface/accelerate/default_config.yaml") as f:
    data = json.load(f)
    config.num_processes = data["num_processes"]


# In[4]:


config.sorted_slice_sample_size = int((config.effective_train_batch_size / config.gradient_accumulation_steps) / config.num_processes)


# In[5]:


if config.debug:
    config.sorted_slice_sample_size = 2
    config.num_inference_steps=1
    config.train_batch_size = 1
    config.eval_batch_size = 1 
    config.eval_loss_timesteps = [20]
    config.train_connected_masks=False
    config.eval_connected_masks=False
    config.evaluate_num_batches=1
    config.dataset_train_path = "./datasets/synthesis/dataset_eval/imgs"
    config.segm_train_path = "./datasets/synthesis/dataset_eval/segm"
    config.masks_train_path = "./datasets/synthesis/dataset_eval/masks"
    config.num_dataloader_workers = 1


# In[6]:


print(f"Start training with batch size {config.sorted_slice_sample_size}, {config.gradient_accumulation_steps} accumulation steps and {config.num_processes} process(es)")


# In[7]:


from custom_modules import DatasetMRI2D, DatasetMRI3D, ScaleDecorator
from pathlib import Path
from torchvision import transforms 

#add augmentation
transformations = None
if config.brightness_augmentation:
    transformations = transforms.RandomApply([ScaleDecorator(transforms.ColorJitter(brightness=1))], p=0.5)

#create dataset
datasetTrain = DatasetMRI2D(root_dir_img=Path(config.dataset_train_path), restriction=config.restrict_train_slices, root_dir_masks=Path(config.masks_train_path), connected_masks=config.train_connected_masks, target_shape=config.target_shape, transforms=transformations, sorted_slice_sample_size=config.sorted_slice_sample_size, restrict_mask_to_wm=config.restrict_mask_to_wm, proportion_training_circular_masks=config.proportion_training_circular_masks)
datasetEvaluation = DatasetMRI2D(root_dir_img=Path(config.dataset_eval_path), restriction=config.restrict_eval_slices, root_dir_masks=Path(config.masks_eval_path), connected_masks=config.eval_connected_masks, target_shape=config.target_shape, sorted_slice_sample_size=config.sorted_slice_sample_size)
dataset3DEvaluation = DatasetMRI3D(root_dir_img=Path(config.dataset_eval_path), root_dir_masks=Path(config.masks_eval_path), connected_masks=config.eval_connected_masks, target_shape=config.target_shape)


# ### Prepare Training

# In[8]:


#create model
from custom_modules import UNet2DModel

model = UNet2DModel(
    sample_size=config.unet_img_shape,  # the target image resolution
    in_channels=3,  # the number of input channels, 3 for RGB images
    out_channels=config.channels,  # the number of output channels
    layers_per_block=2,  # how many ResNet layers to use per UNet block
    block_out_channels=(128, 128, 256, 256, 512, 512),  # the number of output channels for each UNet block
    down_block_types=(
        "Pseudo3DDownBlock2D",  # a regular ResNet downsampling block
        "Pseudo3DDownBlock2D",
        "Pseudo3DDownBlock2D",
        "Pseudo3DDownBlock2D",
        "Pseudo3DAttnDownBlock2D",  # a ResNet downsampling block with spatial self-attention
        "Pseudo3DAttnDownBlock2D",
    ),
    up_block_types=(
        "Pseudo3DAttnUpBlock2D",  # a regular ResNet upsampling block
        "Pseudo3DAttnUpBlock2D",  # a ResNet upsampling block with spatial self-attention
        "Pseudo3DUpBlock2D",
        "Pseudo3DUpBlock2D",
        "Pseudo3DUpBlock2D",
        "Pseudo3DUpBlock2D",
    ),
)

config.model = "BigPseudo3DUNet2DModel"


# In[9]:


#setup noise scheduler
import torch
from PIL import Image
from diffusers import DDIMScheduler

noise_scheduler = DDIMScheduler(num_train_timesteps=1000)
#sample_image = datasetTrain[0]['gt_image'].unsqueeze(0)
#noise = torch.randn(sample_image.shape)
#timesteps = torch.LongTensor([50])
#noisy_image = noise_scheduler.add_noise(sample_image, noise, timesteps)

config.noise_scheduler = "DDIMScheduler(num_train_timesteps=1000)"


# In[10]:


# setup lr scheduler
from diffusers.optimization import get_cosine_schedule_with_warmup
import math

optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate)
lr_scheduler = get_cosine_schedule_with_warmup(
    optimizer=optimizer,
    num_warmup_steps=config.lr_warmup_steps,
    num_training_steps=(math.ceil(len(datasetTrain)/config.train_batch_size) * config.num_epochs), # num_iterations per epoch * num_epochs
)

config.lr_scheduler = "cosine_schedule_with_warmup"


# In[11]:


from custom_modules import TrainingConditional, DDIMInpaintPipeline, Evaluation2DFilling, Evaluation3DFilling
from custom_modules import PipelineFactories 

args = {
    "config": config, 
    "model": model, 
    "noise_scheduler": noise_scheduler, 
    "optimizer": optimizer, 
    "lr_scheduler": lr_scheduler, 
    "datasetTrain": datasetTrain, 
    "datasetEvaluation": datasetEvaluation, 
    "dataset3DEvaluation": dataset3DEvaluation,  
    "evaluation2D": Evaluation2DFilling,
    "evaluation3D": Evaluation3DFilling,
    "pipelineFactory": PipelineFactories.get_ddim_inpaint_pipeline,
    "multi_sample": config.sorted_slice_sample_size > 1,
    "min_snr_loss":config.use_min_snr_loss,}
trainingSynthesis = TrainingConditional(**args)


# In[ ]:


if config.mode == "train":
    trainingSynthesis.train()


# In[ ]:


if config.mode == "eval":
    trainingSynthesis.config.deactivate3Devaluation = False
    pipeline = DDIMInpaintPipeline.from_pretrained(config.output_dir) 
    trainingSynthesis.evaluate(pipeline, deactivate_save_model=True)


# In[ ]:


import os
import csv
from itertools import product
import matplotlib.pyplot as plt

if config.mode == "tuning_parameters":
    trainingSynthesis.config.deactivate3Devaluation = False
    pipeline = GuidedPipelineConditional.from_pretrained(config.output_dir)
    original_output_dir = config.output_dir 
    timesteps = [1, 3, 5]
    resample_step = [1, 3, 5]
    parameters = product(timesteps, resample_step) 
    
    for timestep, resample_step in parameters:
        print("Begin evaluation for timestep ", timestep, " and resample step ", resample_step)
        trainingSynthesis.config.intermediate_timestep = timestep
        trainingSynthesis.config.jump_length = timestep
        trainingSynthesis.config.jump_n_sample = resample_step
        trainingSynthesis.config.output_dir = original_output_dir + "/tuning_parameters/timestep_" + str(timestep) + "_resample_" + str(resample_step)
        if trainingSynthesis.accelerator.is_main_process:
            os.makedirs(config.output_dir, exist_ok=True)
            trainingSynthesis.log_meta_logs()
        trainingSynthesis.evaluate(pipeline, deactivate_save_model=True) 

    # plot lpips score vs  
    folder_list = list(Path().glob(original_output_dir + "/tuning_parameters/timestep_*"))
    lpips = []
    labels = [] 
    for folder in folder_list:
        timestep = str(folder).split("_")[-3] 
        resample_step = str(folder).split("_")[-1]
        with open(folder / "metrics.csv", 'r') as fp: 
            _ = fp.readline()
            csv_metrics = fp.readline()
            reader = csv_metrics.split(',') 
            for metric in reader:
                if metric != "":
                    name, value = metric.split(':')
                    if name == "lpips":
                        lpips.append(float(value))
                        labels.append(timestep + "_" + resample_step) 
    plt.clf()
    plt.bar(labels, lpips) 
    plt.savefig(original_output_dir + '/lpips_parameters.png')


# In[ ]:


print("Finished Training")
