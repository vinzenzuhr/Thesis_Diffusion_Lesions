from Evaluation2D import Evaluation2D
import torch
import numpy as np 

class Evaluation2DFilling(Evaluation2D):
    def __init__(self, config, pipeline, dataloader, tb_summary, accelerator, _get_training_input):
        super().__init__(config, pipeline, dataloader, tb_summary, accelerator, _get_training_input) 
    
    def _get_image_lists(self, images, clean_images, masks, batch):
        # save last batch as sample images
        masked_images = clean_images*(1-masks)
        
        # change range from [-1,1] to [0,1]
        images = (images+1)/2
        masked_images = (masked_images+1)/2
        clean_images = (clean_images+1)/2
        # change binary image from 0,1 to 0,255
        masks = masks*255


        list = [images, masked_images, clean_images, masks]
        title_list = ["images", "masked_images", "clean_images", "masks"] 
        return list, title_list

    def _start_pipeline(self, batch, parameters={}): 
        clean_images = batch["gt_image"]
        masks = batch["mask"] 
        voided_images = clean_images*(1-masks)
        
        print(2) 
        inpainted_images = self.pipeline(
            voided_images,
            masks,
            generator=torch.cuda.manual_seed_all(self.config.seed), 
            num_inference_steps = self.config.num_inference_steps,
            **parameters
        ).images
        print(3) 
        #inpainted_images = torch.from_numpy(inpainted_images).to(clean_images.device) 
        return inpainted_images, clean_images, masks
    
