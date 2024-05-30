#setup evaluation

from custom_modules import EvaluationUtils 
from abc import ABC, abstractmethod 
import os
import torch.nn.functional as F 
import torch  
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
from torchvision.transforms.functional import to_pil_image
from tqdm.auto import tqdm

class Evaluation2D(ABC):
    def __init__(self, config, eval_dataloader, train_dataloader, tb_summary, accelerator):
        self.config = config 
        self.eval_dataloader = eval_dataloader 
        self.train_dataloader = train_dataloader
        self.tb_summary = tb_summary
        self.accelerator = accelerator 
        self.lpips_metric = LearnedPerceptualImagePatchSimilarity(net_type='alex').to(self.accelerator.device)
        self.best_val_loss = float("inf")

    def _calc_lpip(self, images_1, images_2):

        # create rectangular bounding_boxes
        #all_bounding_boxes = masks_to_boxes(masks.squeeze(dim=1)).to(torch.int32) 
        # returns a [N, 4] tensor containing bounding boxes. The boxes are in (x1, y1, x2, y2) format with 0 <= x1 < x2 and 0 <= y1 < y2.
        #assert all_bounding_boxes.shape[0] == masks.shape[0], "Number of bounding boxes expected to match number of masks."
        
        #calculcate lpips for every image and take average
        lpips = 0
        for i in range(images_1.shape[0]):
            #mask = torch.zeros_like(masks, dtype=torch.bool)
            #mask[i, :, all_bounding_boxes[i][1]:all_bounding_boxes[i][3], all_bounding_boxes[i][0]:all_bounding_boxes[i][2]] = True #TODO: check if correct coordinates
            #width = all_bounding_boxes[i][2] - all_bounding_boxes[i][0]
            #height = all_bounding_boxes[i][3] - all_bounding_boxes[i][1] 
            #img1 = images_1[mask].reshape(1, 1, height, width).expand(-1, 3, -1, -1)
            #img2 = images_2[mask].reshape(1, 1, height, width).expand(-1, 3, -1, -1)
            lpips += self.lpips_metric(images_1[i].unsqueeze(0).expand(-1, 3, -1, -1), images_2[i].unsqueeze(0).expand(-1, 3, -1, -1))
        lpips /= images_1.shape[0]

        return lpips

    #def _reset_seed(self, seed): 
    #    np.random.seed(seed) 
    #    torch.manual_seed(seed)
    #    torch.cuda.manual_seed_all(seed)
    #    random.seed(seed)
    #    return

    @abstractmethod
    def _start_pipeline(self, pipeline, batch, generator, parameters):
        pass

    def evaluate(self, pipeline, global_step, _get_training_input, parameters={}, deactivate_save_model=False):
        #initialize metrics
        metrics = dict()
        metric_list = ["ssim_full", "ssim_out", "ssim_in", "mse_full", "mse_out", "mse_in", "psnr_full", "psnr_out", "psnr_in", "val_loss", "lpips"] 
        for t in self.config.eval_loss_timesteps:
            metric_list.append(f"val_loss_{t}")
            metric_list.append(f"train_loss_{t}")
        for metric in metric_list:
            metrics[metric] = 0 


        eval_generator = torch.Generator(device=self.accelerator.device).manual_seed(self.config.seed)
            
        # calc t specific training loss
        timesteps = torch.tensor(self.config.eval_loss_timesteps, dtype=torch.int, device=self.accelerator.device)
        max_iter = len(self.eval_dataloader) if self.config.evaluate_num_batches == -1 else self.config.evaluate_num_batches
        for n_iter, batch_train in enumerate(self.train_dataloader):
            if n_iter >= max_iter:
                break    
            input, noise, timesteps = _get_training_input(batch_train, generator=eval_generator, timesteps=timesteps)
            noise_pred = pipeline.unet(input, timesteps, return_dict=False)[0]
            for i, t in enumerate(timesteps):
                loss = F.mse_loss(noise_pred[i], noise[i])
                all_loss = self.accelerator.gather_for_metrics(loss).mean() 
                metrics[f"train_loss_{t}"] += all_loss
            #free up memory
            del input, noise, timesteps, noise_pred, loss, all_loss

         
        self.progress_bar = tqdm(total=len(self.eval_dataloader) if self.config.evaluate_num_batches == -1 else self.config.evaluate_num_batches, disable=not self.accelerator.is_local_main_process) 
        self.progress_bar.set_description(f"Evaluation 2D")  
        #self._reset_seed(self.config.seed)
        if hasattr(self.eval_dataloader._index_sampler, "sampler"):
            self.eval_dataloader._index_sampler.sampler.generator.manual_seed(self.config.seed)
        else:
            self.eval_dataloader._index_sampler.batch_sampler.sampler.generator.manual_seed(self.config.seed)
        
        for n_iter, batch in enumerate(self.eval_dataloader):
            # calc validation loss
            timesteps = torch.tensor(self.config.eval_loss_timesteps, dtype=torch.int, device=self.accelerator.device)
            input, noise, timesteps = _get_training_input(batch, generator=eval_generator)
            noise_pred = pipeline.unet(input, timesteps, return_dict=False)[0]
            loss = F.mse_loss(noise_pred, noise)
            all_loss = self.accelerator.gather_for_metrics(loss).mean() 
            metrics["val_loss"] += all_loss 
            #free up memory
            del input, noise, timesteps, noise_pred, loss, all_loss

            # calc t specific validation loss
            input, noise, timesteps = _get_training_input(batch, generator=eval_generator, timesteps=timesteps)
            noise_pred = pipeline.unet(input, timesteps, return_dict=False)[0]
            for i, t in enumerate(timesteps):
                loss = F.mse_loss(noise_pred[i], noise[i])
                all_loss = self.accelerator.gather_for_metrics(loss).mean() 
                metrics[f"val_loss_{t}"] += all_loss 
            #free up memory
            del input, noise, timesteps, noise_pred, loss, all_loss 


            torch.cuda.empty_cache() 
            # run pipeline. The returned masks can be either existing lesions or the synthetic ones
            images, clean_images, masks = self._start_pipeline(
                pipeline,  
                batch,
                eval_generator,
                parameters
            ) 
            # transform from B x H x W x C to B x C x H x W 
            #images = torch.permute(images, (0, 3, 1, 2))

            # calculate metrics
            all_clean_images = self.accelerator.gather_for_metrics(clean_images)
            all_images = self.accelerator.gather_for_metrics(images)
            all_masks = self.accelerator.gather_for_metrics(masks)

            metrics["lpips"] += self._calc_lpip(all_clean_images, all_images)
            new_metrics = EvaluationUtils.calc_metrics(all_clean_images, all_images, all_masks)
            for key, value in new_metrics.items(): 
                metrics[key] += value

            self.progress_bar.update(1)
            
            if (self.config.evaluate_num_batches != -1) and (n_iter >= self.config.evaluate_num_batches-1):
                break 
        
        # calculate average metrics
        for key, value in metrics.items():
            if self.config.evaluate_num_batches == -1:
                metrics[key] /= len(self.eval_dataloader)
            else:
                metrics[key] /= self.config.evaluate_num_batches

        if self.accelerator.is_main_process:
            # log metrics
            EvaluationUtils.log_metrics(self.tb_summary, global_step, metrics, self.config)

            # save last batch as sample images
            list, title_list = self._get_image_lists(images, clean_images, masks, batch)
            image_list = [[to_pil_image(x, mode="L") for x in images] for images in list]
            EvaluationUtils.save_image(image_list, title_list, os.path.join(self.config.output_dir, "samples_2D"), global_step, self.config.unet_img_shape)

            # save model
            if not deactivate_save_model:# and (self.best_val_loss > metrics["val_loss"]):
                #self.best_val_loss = metrics["val_loss"]
                pipeline.save_pretrained(self.config.output_dir)
                print("model saved")