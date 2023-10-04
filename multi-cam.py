import os
import cv2
import time
import tqdm
import numpy as np
import dearpygui.dearpygui as dpg

import torch
import torch.nn.functional as F

import rembg

from cam_utils import orbit_camera, OrbitCamera
from gs_renderer import Renderer, MiniCam

from grid_put import mipmap_linear_grid_put_2d
from mesh import Mesh, safe_normalize

import math

import pickle
def read_pickle(file_path):
    with open(file_path, 'rb') as f:
        return pickle.load(f)

class GUI:
    def __init__(self, opt):
        # shared with the trainer's opt to support in-place modification of rendering parameters.
        self.opt = opt
        self.gui = opt.gui  # enable gui
        self.W = opt.W
        self.H = opt.H
        self.cam = OrbitCamera(opt.W, opt.H, r=opt.radius, fovy=opt.fovy)

        self.mode = "image"
        self.seed = "random"

        self.buffer_image = np.ones((self.W, self.H, 3), dtype=np.float32)
        self.need_update = True  # update buffer_image

        # models
        self.device = torch.device("cuda")
        self.bg_remover = None

        self.guidance_sd = None
        self.guidance_zero123 = None

        self.enable_sd = False
        self.enable_zero123 = False

        # renderer
        self.renderer = Renderer(sh_degree=self.opt.sh_degree)
        self.gaussain_scale_factor = 1

        # input image
        self.input_img = None
        self.input_mask = None
        self.input_img_torch = None
        self.input_mask_torch = None
        self.overlay_input_img = False
        self.overlay_input_img_ratio = 0.5

        '''
        # depth map
        self.input_depth_map_torch = None  # New variable to hold the depth-map tensor
        self.input_depth_map = None
        # Load depth-map if provided
        if opt.depth_map is not None:
            self.input_depth_map = self.load_depth_map(opt.depth_map)
        '''

        # input text
        self.prompt = ""
        self.negative_prompt = ""

        # training stuff
        self.training = False
        self.optimizer = None
        self.step = 0
        self.train_steps = 1  # steps per rendering loop

        # load input data from cmdline
        if self.opt.input is not None:
            self.load_input(self.opt.input)

        # override prompt from cmdline
        if self.opt.prompt is not None:
            self.prompt = self.opt.prompt

        # override if provide a checkpoint
        if self.opt.load is not None:
            self.renderer.initialize(self.opt.load)
        else:
            # initialize gaussians to a blob
            self.renderer.initialize(num_pts=self.opt.num_pts)

        if self.gui:
            dpg.create_context()
            self.register_dpg()
            self.test_step()

    def __del__(self):
        if self.gui:
            dpg.destroy_context()

    def seed_everything(self):
        try:
            seed = int(self.seed)
        except:
            seed = np.random.randint(0, 1000000)

        os.environ["PYTHONHASHSEED"] = str(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = True

        self.last_seed = seed

    def prepare_train(self):

        self.step = 0

        # setup training
        self.renderer.gaussians.training_setup(self.opt)
        # do not do progressive sh-level
        self.renderer.gaussians.active_sh_degree = self.renderer.gaussians.max_sh_degree
        self.optimizer = self.renderer.gaussians.optimizer

        # default camera
        pose = orbit_camera(self.opt.elevation, 0, self.opt.radius)
        self.fixed_cam = MiniCam(
            pose,
            self.opt.ref_size,
            self.opt.ref_size,
            self.cam.fovy,
            self.cam.fovx,
            self.cam.near,
            self.cam.far,
        )


        #load camera data from poses file
        # Reading camera data
        K, azs, _, _, poses = read_pickle(self.opt.camerasFile)

        # If the length of azs and poses match, you can iterate over them to create multiple cameras
        cameras = []
        for i,(az, pose) in enumerate(zip(azs, poses)):
            angle = 2*math.pi*i/360
            pose = orbit_camera(self.opt.elevation, angle, self.opt.radius)
            cameras.append(MiniCam(
                pose,
                self.opt.ref_size,
                self.opt.ref_size,
                self.cam.fovy,
                self.cam.fovx,
                self.cam.near,
                self.cam.far,
            ))

        self.cameras = cameras

        #load images from images file
        multiImages, multiMasks = self.load_multi_image(self.opt.imagesFile)
        self.multiImages = multiImages
        self.multiMasks = multiMasks

        #convert to torch
        self.multiImagesTorch = []
        self.multiMasksTorch = []
        for i in range(16):
            self.multiImagesTorch.append(torch.from_numpy(self.multiImages[i]).permute(2, 0, 1).unsqueeze(0).to(self.device))
            self.multiMasksTorch.append(torch.from_numpy(self.multiMasks[i]).permute(2, 0, 1).unsqueeze(0).to(self.device))
            #reize to ref_size
            self.multiImagesTorch[i] = F.interpolate(self.multiImagesTorch[i], (self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)
            self.multiMasksTorch[i] = F.interpolate(self.multiMasksTorch[i], (self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)


        self.enable_sd = self.opt.lambda_sd > 0 and self.prompt != ""
        self.enable_zero123 = self.opt.lambda_zero123 > 0 and self.input_img is not None

        # lazy load guidance model
        if self.guidance_sd is None and self.enable_sd:
            print(f"[INFO] loading SD...")
            from guidance.sd_utils import StableDiffusion
            self.guidance_sd = StableDiffusion(self.device)
            print(f"[INFO] loaded SD!")

        if self.guidance_zero123 is None and self.enable_zero123:
            print(f"[INFO] loading zero123...")
            from guidance.zero123_utils import Zero123
            self.guidance_zero123 = Zero123(self.device)
            print(f"[INFO] loaded zero123!")

        # input image
        if self.input_img is not None:
            self.input_img_torch = torch.from_numpy(self.input_img).permute(
                2, 0, 1).unsqueeze(0).to(self.device)
            self.input_img_torch = F.interpolate(self.input_img_torch, (
                self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)

            self.input_mask_torch = torch.from_numpy(
                self.input_mask).permute(2, 0, 1).unsqueeze(0).to(self.device)
            self.input_mask_torch = F.interpolate(self.input_mask_torch, (
                self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)

        '''
        # depth map
        if self.input_depth_map is not None:
            print("Setting up depth map")
            self.input_depth_map_torch = torch.from_numpy(
                self.input_depth_map).unsqueeze(0).unsqueeze(0).to(self.device)
            self.input_depth_map_torch = F.interpolate(self.input_depth_map_torch, (
                self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)
        else:
            print("No depth map provided",
                  self.opt.depth_map, self.input_depth_map)
        '''

        # prepare embeddings
        with torch.no_grad():

            if self.enable_sd:
                self.guidance_sd.get_text_embeds(
                    [self.prompt], [self.negative_prompt])

            if self.enable_zero123:
                self.guidance_zero123.get_img_embeds(self.input_img_torch)

    def get_fatness(self, gaussians, cam_matrix):
        # Project Gaussian centers to the camera coordinate system
        projected_centers = torch.matmul(
            gaussians.get_xyz, cam_matrix[:3, :3].T) + cam_matrix[:3, 3]

        # Compute the location in the dimension the camera is facing (z-axis in camera coordinate system)
        z_locations = projected_centers[:, 2]

        # Compute the standard deviation in this dimension
        std_dev = torch.std(z_locations)

        return std_dev

    def get_column_fatness(self, gaussians, cam_matrix, grid_size=16):
        # Get Gaussian centers and transform to camera coordinate system
        xyzs = gaussians.get_xyz
        transformed_xyzs = torch.matmul(
            xyzs, cam_matrix[:3, :3].T) + cam_matrix[:3, 3]
        mask = ~torch.isnan(transformed_xyzs).any(dim=1)
        transformed_xyzs = transformed_xyzs[mask]

        # Create a grid based on x-y coordinates
        min_x = torch.min(transformed_xyzs[:, 0])
        max_x = torch.max(transformed_xyzs[:, 0])
        min_y = torch.min(transformed_xyzs[:, 1])
        max_y = torch.max(transformed_xyzs[:, 1])
        x_linspace = torch.linspace(min_x.item(), max_x.item(), grid_size)
        y_linspace = torch.linspace(min_y.item(), max_y.item(), grid_size)

        grid_x, grid_y = torch.meshgrid(x_linspace, y_linspace)

        fatness_scores = []
        total_gaussians = 0

        # Loop through each cell in the grid
        for i in range(grid_size - 1):
            for j in range(grid_size - 1):
                # Define the boundaries of the current cell
                # x_min, x_max = grid_x[i, j], grid_x[i+1, j]
                # y_min, y_max = grid_y[i, j], grid_y[i, j+1]
                x_min, x_max = x_linspace[i], x_linspace[i+1]
                y_min, y_max = y_linspace[j], y_linspace[j+1]

                # Find the Gaussians that fall into the current cell
                mask = (transformed_xyzs[:, 0] >= x_min) & (transformed_xyzs[:, 0] <= x_max) & \
                    (transformed_xyzs[:, 1] >= y_min) & (
                        transformed_xyzs[:, 1] <= y_max)

                # Compute the fatness (std deviation) of the z-values of the Gaussians in this cell
                num_gaussians = mask.sum().item()

                # print(i,j,num_gaussians)

                if num_gaussians > 1:
                    column_z_values = transformed_xyzs[mask, 2]
                    column_fatness = torch.std(column_z_values)
                    fatness_scores.append(column_fatness * num_gaussians)
                    total_gaussians += num_gaussians

        # print("tg",total_gaussians)

        # Compute the weighted mean fatness score
        if fatness_scores:
            weighted_mean_fatness = torch.sum(
                torch.stack(fatness_scores)) / total_gaussians
        else:
            weighted_mean_fatness = torch.tensor(0.0)

        return weighted_mean_fatness

    def train_step(self):
        starter = torch.cuda.Event(enable_timing=True)
        ender = torch.cuda.Event(enable_timing=True)
        starter.record()

        for _ in range(self.train_steps):

            self.step += 1
            step_ratio = min(1, self.step / self.opt.iters)

            # update lr
            self.renderer.gaussians.update_learning_rate(self.step)

            loss = 0

            # known view
            if self.input_img_torch is not None:
                cur_cam = self.fixed_cam
                out = self.renderer.render(cur_cam)

                # rgb loss
                image = out["image"].unsqueeze(0)  # [1, 3, H, W] in [0, 1]
                loss = loss + self.opt.known_view_weight * step_ratio * \
                    F.mse_loss(image, self.input_img_torch)

                # mask loss
                mask = out["alpha"].unsqueeze(0)  # [1, 1, H, W] in [0, 1]
                loss = loss + self.opt.known_view_mask_weight * step_ratio * \
                    F.mse_loss(mask, self.input_mask_torch)
                

            #multi-view
            for i in range(16):
                cur_cam = self.cameras[i]
                out = self.renderer.render(cur_cam)

                # rgb loss
                image = self.multiImagesTorch[i]

                #print('about to die 0',image)

                loss = loss + self.opt.mv_weight * step_ratio * \
                    F.mse_loss(image, self.multiImagesTorch[i])
                
                # mask loss
                mask = self.multiMasksTorch[i]

                #print('about to die 1',mask)

                loss = loss + self.opt.mv_mask_weight * step_ratio * \
                    F.mse_loss(mask, self.multiMasksTorch[i])


            '''
            # Depth map loss
            depth_loss = 0
            if self.input_depth_map_torch is not None and self.input_mask_torch is not None:
                cur_cam = self.fixed_cam
                out = self.renderer.render(cur_cam)
                # Use 'depth' instead of 'depth_map'
                depth_map = out["depth"].unsqueeze(0)

                #normally depth map is in range -1,1

                # depth is opposite of distance
                depth_map = (2 - depth_map)/2

                #so now with mean=0, scale=1, it should be in the range [0,1]

                adjusted_depth_map = (
                    depth_map - self.opt.depth_mean) * self.opt.depth_scale

                # Apply mask to depth_map and input_depth_map
                masked_adjusted_depth_map = adjusted_depth_map * self.input_mask_torch
                masked_input_depth_map = self.input_depth_map_torch * self.input_mask_torch

                #compute max and min of masked_adjusted_depth_map
                max_depth = torch.max(masked_adjusted_depth_map)
                min_depth = torch.min(masked_adjusted_depth_map)
                #and also for masked_input_depth_map
                max_input_depth = torch.max(masked_input_depth_map)
                min_input_depth = torch.min(masked_input_depth_map)
                #print("max_depth", max_depth, "min_depth", min_depth, "max_input_depth", max_input_depth, "min_input_depth", min_input_depth)
                

                depth_loss = F.mse_loss(
                    masked_adjusted_depth_map, masked_input_depth_map)

                # print("here", depth_loss)

                loss = loss + self.opt.depth_weight * depth_loss  # Weighted depth_loss
            else:
                # print("huh",self.input_depth_map_torch,self.input_mask_torch)
                pass
                
            '''

            # novel view (manual batch)
            render_resolution = 128 if step_ratio < 0.3 else (
                256 if step_ratio < 0.6 else 512)
            images = []
            vers, hors, radii = [], [], []
            # avoid too large elevation (> 80 or < -80), and make sure it always cover [-30, 30]
            min_ver = max(min(-30, -30 - self.opt.elevation), -
                          80 - self.opt.elevation)
            max_ver = min(max(30, 30 - self.opt.elevation),
                          80 - self.opt.elevation)
            for _ in range(self.opt.batch_size):

                # render random view
                ver = np.random.randint(min_ver, max_ver)
                hor = np.random.randint(-180, 180)
                radius = 0

                vers.append(ver)
                hors.append(hor)
                radii.append(radius)

                pose = orbit_camera(self.opt.elevation + ver,
                                    hor, self.opt.radius + radius)

                cur_cam = MiniCam(
                    pose,
                    render_resolution,
                    render_resolution,
                    self.cam.fovy,
                    self.cam.fovx,
                    self.cam.near,
                    self.cam.far,
                )

                invert_bg_color = np.random.rand() > self.opt.invert_bg_prob
                out = self.renderer.render(
                    cur_cam, invert_bg_color=invert_bg_color)

                image = out["image"].unsqueeze(0)  # [1, 3, H, W] in [0, 1]
                images.append(image)

            images = torch.cat(images, dim=0)

            # import kiui
            # kiui.lo(hor, ver)
            # kiui.vis.plot_image(image)

            # guidance loss
            if self.enable_sd:
                loss = loss + self.opt.lambda_sd * \
                    self.guidance_sd.train_step(images, step_ratio)

            if self.enable_zero123:
                loss = loss + self.opt.lambda_zero123 * \
                    self.guidance_zero123.train_step(
                        images, vers, hors, radii, step_ratio)


            '''
            # fatness
            fatness_score = self.get_fatness(
                self.renderer.gaussians, self.fixed_cam.world_view_transform)
            # fatness_score = self.get_column_fatness(self.renderer.gaussians, self.fixed_cam.world_view_transform)
            if hasattr(self.opt, 'lambda_fatness') and hasattr(self.opt, 'ideal_fatness'):
                loss = loss + self.opt.lambda_fatness * \
                    (fatness_score - self.opt.ideal_fatness).abs()
            '''

            # optimize step
            loss.backward()
            self.optimizer.step()
            self.optimizer.zero_grad()

            # densify and prune
            if self.step >= self.opt.density_start_iter and self.step <= self.opt.density_end_iter:
                viewspace_point_tensor, visibility_filter, radii = out[
                    "viewspace_points"], out["visibility_filter"], out["radii"]
                self.renderer.gaussians.max_radii2D[visibility_filter] = torch.max(
                    self.renderer.gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                self.renderer.gaussians.add_densification_stats(
                    viewspace_point_tensor, visibility_filter)

                if self.step % self.opt.densification_interval == 0:
                    # size_threshold = 20 if self.step > self.opt.opacity_reset_interval else None
                    self.renderer.gaussians.densify_and_prune(
                        self.opt.densify_grad_threshold, min_opacity=0.01, extent=0.5, max_screen_size=1)

                if self.step % self.opt.opacity_reset_interval == 0:
                    self.renderer.gaussians.reset_opacity()

        ender.record()
        torch.cuda.synchronize()
        t = starter.elapsed_time(ender)

        self.need_update = True

        if self.gui:
            dpg.set_value("_log_train_time", f"{t:.4f}ms")
            dpg.set_value(
                "_log_train_log",
                f"step = {self.step: 5d} (+{self.train_steps: 2d}) loss = {loss.item():.4f}",
            )

        # dynamic train steps (no need for now)
        # max allowed train time per-frame is 500 ms
        # full_t = t / self.train_steps * 16
        # train_steps = min(16, max(4, int(16 * 500 / full_t)))
        # if train_steps > self.train_steps * 1.2 or train_steps < self.train_steps * 0.8:
        #     self.train_steps = train_steps

    @torch.no_grad()
    def test_step(self):
        # ignore if no need to update
        if not self.need_update:
            return

        starter = torch.cuda.Event(enable_timing=True)
        ender = torch.cuda.Event(enable_timing=True)
        starter.record()

        # should update image
        if self.need_update:
            # render image

            cur_cam = MiniCam(
                self.cam.pose,
                self.W,
                self.H,
                self.cam.fovy,
                self.cam.fovx,
                self.cam.near,
                self.cam.far,
            )

            out = self.renderer.render(cur_cam, self.gaussain_scale_factor)

            buffer_image = out[self.mode]  # [3, H, W]

            if self.mode in ['depth', 'alpha']:
                buffer_image = buffer_image.repeat(3, 1, 1)
                if self.mode == 'depth':
                    buffer_image = (buffer_image - buffer_image.min()) / \
                        (buffer_image.max() - buffer_image.min() + 1e-20)

            buffer_image = F.interpolate(
                buffer_image.unsqueeze(0),
                size=(self.H, self.W),
                mode="bilinear",
                align_corners=False,
            ).squeeze(0)

            self.buffer_image = (
                buffer_image.permute(1, 2, 0)
                .contiguous()
                .clamp(0, 1)
                .contiguous()
                .detach()
                .cpu()
                .numpy()
            )

            # display input_image
            if self.overlay_input_img and self.input_img is not None:
                self.buffer_image = (
                    self.buffer_image * (1 - self.overlay_input_img_ratio)
                    + self.input_img * self.overlay_input_img_ratio
                )

            self.need_update = False

        ender.record()
        torch.cuda.synchronize()
        t = starter.elapsed_time(ender)

        if self.gui:
            dpg.set_value("_log_infer_time", f"{t:.4f}ms ({int(1000/t)} FPS)")
            dpg.set_value(
                "_texture", self.buffer_image
            )  # buffer must be contiguous, else seg fault!

    def load_input(self, file):
        # load image
        print(f'[INFO] load image from {file}...')
        img = cv2.imread(file, cv2.IMREAD_UNCHANGED)
        if img.shape[-1] == 3:
            if self.bg_remover is None:
                self.bg_remover = rembg.new_session()
            img = rembg.remove(img, session=self.bg_remover)

        img = cv2.resize(img, (self.W, self.H), interpolation=cv2.INTER_AREA)
        img = img.astype(np.float32) / 255.0

        self.input_mask = img[..., 3:]
        # white bg
        self.input_img = img[..., :3] * self.input_mask + (1 - self.input_mask)
        # bgr to rgb
        self.input_img = self.input_img[..., ::-1].copy()

        # load prompt
        file_prompt = file.replace("_rgba.png", "_caption.txt")
        if os.path.exists(file_prompt):
            print(f'[INFO] load prompt from {file_prompt}...')
            with open(file_prompt, "r") as f:
                self.prompt = f.read().strip()

    #this function loads an image that is size 256x(256*16) and breaks it into 16 images of size 256x256
    def load_multi_image(self, file):
        #load image
        print(f'[INFO] load image from {file}...')
        img = cv2.imread(file, cv2.IMREAD_UNCHANGED)

        #print("huh?",img)

        #break into 16 images
        imgs=[]
        for i in range(16):
            imgs.append(img[:,i*256:(i+1)*256,:])

        #remove backgrouns for each img
        for i in range(16):
            if imgs[i].shape[-1] == 3:
                if self.bg_remover is None:
                    self.bg_remover = rembg.new_session()
                imgs[i] = rembg.remove(imgs[i], session=self.bg_remover)

            #resize each image
            imgs[i] = cv2.resize(imgs[i], (self.W, self.H), interpolation=cv2.INTER_AREA)
            imgs[i] = imgs[i].astype(np.float32) / 255.0

            #print("huh?",i,imgs[i])

        masks=[]
        for i in range(16):
            masks.append(imgs[i][..., 3:])
            # white bg
            imgs[i] = imgs[i][..., :3] * masks[i] + (1 - masks[i])
            # bgr to rgb
            imgs[i] = imgs[i][..., ::-1].copy()

        return imgs, masks


    def load_depth_map(self, file):
        print(f'[INFO] load depth map from {file}...')
        depth_map = cv2.imread(file, cv2.IMREAD_UNCHANGED)

        # Take just one channel if the image has more than one.
        if len(depth_map.shape) == 3:
            depth_map = depth_map[:, :, 0]

        depth_map = cv2.resize(depth_map, (self.W, self.H),
                               interpolation=cv2.INTER_AREA)
        self.input_depth_map = depth_map.astype(
            np.float32) / 255.0  # Assuming 8-bit depth map

        print("depth_map", self.input_depth_map)

        return self.input_depth_map
    



    @torch.no_grad()
    def save_model(self, mode='geo', texture_size=1024):
        os.makedirs(self.opt.outdir, exist_ok=True)
        if mode == 'geo':
            path = os.path.join(
                self.opt.outdir, self.opt.save_path + '_mesh.ply')
            mesh = self.renderer.gaussians.extract_mesh(
                path, self.opt.density_thresh)
            mesh.write_ply(path)

        elif mode == 'geo+tex':
            path = os.path.join(
                self.opt.outdir, self.opt.save_path + '_mesh.obj')
            mesh = self.renderer.gaussians.extract_mesh(
                path, self.opt.density_thresh)

            # perform texture extraction
            print(f"[INFO] unwrap uv...")
            h = w = texture_size
            mesh.auto_uv()
            mesh.auto_normal()

            albedo = torch.zeros(
                (h, w, 3), device=self.device, dtype=torch.float32)
            cnt = torch.zeros((h, w, 1), device=self.device,
                              dtype=torch.float32)

            # self.prepare_train() # tmp fix for not loading 0123
            # vers = [0]
            # hors = [0]
            vers = [0] * 8 + [-45] * 8 + [45] * 8 + [-89.9, 89.9]
            hors = [0, 45, -45, 90, -90, 135, -135, 180] * 3 + [0, 0]

            render_resolution = 512

            import nvdiffrast.torch as dr

            if not self.opt.force_cuda_rast and (not self.opt.gui or os.name == 'nt'):
                glctx = dr.RasterizeGLContext()
            else:
                glctx = dr.RasterizeCudaContext()

            for ver, hor in zip(vers, hors):
                # render image
                pose = orbit_camera(ver, hor, self.cam.radius)

                cur_cam = MiniCam(
                    pose,
                    render_resolution,
                    render_resolution,
                    self.cam.fovy,
                    self.cam.fovx,
                    self.cam.near,
                    self.cam.far,
                )

                cur_out = self.renderer.render(cur_cam)

                rgbs = cur_out["image"].unsqueeze(0)  # [1, 3, H, W] in [0, 1]

                # enhance texture quality with zero123 [not working well]
                # if self.opt.guidance_model == 'zero123':
                #     rgbs = self.guidance.refine(rgbs, [ver], [hor], [0])
                # import kiui
                # kiui.vis.plot_image(rgbs)

                # get coordinate in texture image
                pose = torch.from_numpy(
                    pose.astype(np.float32)).to(self.device)
                proj = torch.from_numpy(
                    self.cam.perspective.astype(np.float32)).to(self.device)

                v_cam = torch.matmul(F.pad(mesh.v, pad=(
                    0, 1), mode='constant', value=1.0), torch.inverse(pose).T).float().unsqueeze(0)
                v_clip = v_cam @ proj.T
                rast, rast_db = dr.rasterize(
                    glctx, v_clip, mesh.f, (render_resolution, render_resolution))

                # [1, H, W, 1]
                depth, _ = dr.interpolate(-v_cam[..., [2]], rast, mesh.f)
                depth = depth.squeeze(0)  # [H, W, 1]

                alpha = (rast[0, ..., 3:] > 0).float()

                uvs, _ = dr.interpolate(mesh.vt.unsqueeze(
                    0), rast, mesh.ft)  # [1, 512, 512, 2] in [0, 1]

                # use normal to produce a back-project mask
                normal, _ = dr.interpolate(
                    mesh.vn.unsqueeze(0).contiguous(), rast, mesh.fn)
                normal = safe_normalize(normal[0])

                # rotated normal (where [0, 0, 1] always faces camera)
                rot_normal = normal @ pose[:3, :3]
                viewcos = rot_normal[..., [2]]

                mask = (alpha > 0) & (viewcos > 0.5)  # [H, W, 1]
                mask = mask.view(-1)

                uvs = uvs.view(-1, 2).clamp(0, 1)[mask]
                rgbs = rgbs.view(3, -1).permute(1, 0)[mask].contiguous()

                # update texture image
                cur_albedo, cur_cnt = mipmap_linear_grid_put_2d(
                    h, w,
                    uvs[..., [1, 0]] * 2 - 1,
                    rgbs,
                    min_resolution=256,
                    return_count=True,
                )

                # albedo += cur_albedo
                # cnt += cur_cnt
                mask = cnt.squeeze(-1) < 0.1
                albedo[mask] += cur_albedo[mask]
                cnt[mask] += cur_cnt[mask]

            mask = cnt.squeeze(-1) > 0
            albedo[mask] = albedo[mask] / cnt[mask].repeat(1, 3)

            mask = mask.view(h, w)

            albedo = albedo.detach().cpu().numpy()
            mask = mask.detach().cpu().numpy()

            # dilate texture
            from sklearn.neighbors import NearestNeighbors
            from scipy.ndimage import binary_dilation, binary_erosion

            inpaint_region = binary_dilation(mask, iterations=32)
            inpaint_region[mask] = 0

            search_region = mask.copy()
            not_search_region = binary_erosion(search_region, iterations=3)
            search_region[not_search_region] = 0

            search_coords = np.stack(np.nonzero(search_region), axis=-1)
            inpaint_coords = np.stack(np.nonzero(inpaint_region), axis=-1)

            knn = NearestNeighbors(n_neighbors=1, algorithm="kd_tree").fit(
                search_coords
            )
            _, indices = knn.kneighbors(inpaint_coords)

            albedo[tuple(inpaint_coords.T)] = albedo[tuple(
                search_coords[indices[:, 0]].T)]

            mesh.albedo = torch.from_numpy(albedo).to(self.device)
            mesh.write(path)

        else:
            path = os.path.join(
                self.opt.outdir, self.opt.save_path + '_model.ply')
            self.renderer.gaussians.save_ply(path)

        print(f"[INFO] save model to {path}.")

    def register_dpg(self):
        # register texture

        with dpg.texture_registry(show=False):
            dpg.add_raw_texture(
                self.W,
                self.H,
                self.buffer_image,
                format=dpg.mvFormat_Float_rgb,
                tag="_texture",
            )

        # register window

        # the rendered image, as the primary window
        with dpg.window(
            tag="_primary_window",
            width=self.W,
            height=self.H,
            pos=[0, 0],
            no_move=True,
            no_title_bar=True,
            no_scrollbar=True,
        ):
            # add the texture
            dpg.add_image("_texture")

        # dpg.set_primary_window("_primary_window", True)

        # control window
        with dpg.window(
            label="Control",
            tag="_control_window",
            width=600,
            height=self.H,
            pos=[self.W, 0],
            no_move=True,
            no_title_bar=True,
        ):
            # button theme
            with dpg.theme() as theme_button:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (23, 3, 18))
                    dpg.add_theme_color(
                        dpg.mvThemeCol_ButtonHovered, (51, 3, 47))
                    dpg.add_theme_color(
                        dpg.mvThemeCol_ButtonActive, (83, 18, 83))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
                    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 3, 3)

            # timer stuff
            with dpg.group(horizontal=True):
                dpg.add_text("Infer time: ")
                dpg.add_text("no data", tag="_log_infer_time")

            def callback_setattr(sender, app_data, user_data):
                setattr(self, user_data, app_data)

            # init stuff
            with dpg.collapsing_header(label="Initialize", default_open=True):

                # seed stuff
                def callback_set_seed(sender, app_data):
                    self.seed = app_data
                    self.seed_everything()

                dpg.add_input_text(
                    label="seed",
                    default_value=self.seed,
                    on_enter=True,
                    callback=callback_set_seed,
                )

                # input stuff
                def callback_select_input(sender, app_data):
                    # only one item
                    for k, v in app_data["selections"].items():
                        dpg.set_value("_log_input", k)
                        self.load_input(v)

                    self.need_update = True

                with dpg.file_dialog(
                    directory_selector=False,
                    show=False,
                    callback=callback_select_input,
                    file_count=1,
                    tag="file_dialog_tag",
                    width=700,
                    height=400,
                ):
                    dpg.add_file_extension("Images{.jpg,.jpeg,.png}")

                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="input",
                        callback=lambda: dpg.show_item("file_dialog_tag"),
                    )
                    dpg.add_text("", tag="_log_input")

                # overlay stuff
                with dpg.group(horizontal=True):

                    def callback_toggle_overlay_input_img(sender, app_data):
                        self.overlay_input_img = not self.overlay_input_img
                        self.need_update = True

                    dpg.add_checkbox(
                        label="overlay image",
                        default_value=self.overlay_input_img,
                        callback=callback_toggle_overlay_input_img,
                    )

                    def callback_set_overlay_input_img_ratio(sender, app_data):
                        self.overlay_input_img_ratio = app_data
                        self.need_update = True

                    dpg.add_slider_float(
                        label="ratio",
                        min_value=0,
                        max_value=1,
                        format="%.1f",
                        default_value=self.overlay_input_img_ratio,
                        callback=callback_set_overlay_input_img_ratio,
                    )

                # prompt stuff

                dpg.add_input_text(
                    label="prompt",
                    default_value=self.prompt,
                    callback=callback_setattr,
                    user_data="prompt",
                )

                dpg.add_input_text(
                    label="negative",
                    default_value=self.negative_prompt,
                    callback=callback_setattr,
                    user_data="negative_prompt",
                )

                
                #set opt
                def callback_set_opt_attr(sender, app_data, user_data):
                    setattr(self.opt, user_data, app_data)

                #add input for elevation
                dpg.add_input_float(label="elevation", default_value=self.opt.elevation,
                                    callback=callback_set_opt_attr, user_data="elevation")


                '''
                # Add input boxes for fatness parameters
                dpg.add_input_float(label="lambda_fatness", default_value=self.opt.lambda_fatness,
                                    callback=callback_set_opt_attr, user_data="lambda_fatness")
                dpg.add_input_float(label="ideal_fatness", default_value=self.opt.ideal_fatness,
                                    callback=callback_set_opt_attr, user_data="ideal_fatness")
                '''    

                '''
                # depth map
                if self.opt.depth_map is not None:
                    # Add input boxes for depth parameters
                    dpg.add_input_float(label="depth_mean", default_value=self.opt.depth_mean,
                                        callback=callback_set_opt_attr, user_data="depth_mean")
                    dpg.add_input_float(label="depth_scale", default_value=self.opt.depth_scale,
                                        callback=callback_set_opt_attr, user_data="depth_scale")
                    dpg.add_input_float(label="depth_weight", default_value=self.opt.depth_weight,
                                        callback=callback_set_opt_attr, user_data="depth_weight")
                '''

                # save current model
                with dpg.group(horizontal=True):
                    dpg.add_text("Save: ")

                    def callback_save(sender, app_data, user_data):
                        self.save_model(mode=user_data)

                    dpg.add_button(
                        label="model",
                        tag="_button_save_model",
                        callback=callback_save,
                        user_data='model',
                    )
                    dpg.bind_item_theme("_button_save_model", theme_button)

                    dpg.add_button(
                        label="geo",
                        tag="_button_save_mesh",
                        callback=callback_save,
                        user_data='geo',
                    )
                    dpg.bind_item_theme("_button_save_mesh", theme_button)

                    dpg.add_button(
                        label="geo+tex",
                        tag="_button_save_mesh_with_tex",
                        callback=callback_save,
                        user_data='geo+tex',
                    )
                    dpg.bind_item_theme(
                        "_button_save_mesh_with_tex", theme_button)

                    dpg.add_input_text(
                        label="",
                        default_value=self.opt.save_path,
                        callback=callback_setattr,
                        user_data="save_path",
                    )

            # training stuff
            with dpg.collapsing_header(label="Train", default_open=True):
                # lr and train button
                with dpg.group(horizontal=True):
                    dpg.add_text("Train: ")

                    def callback_train(sender, app_data):
                        if self.training:
                            self.training = False
                            dpg.configure_item("_button_train", label="start")
                        else:
                            self.prepare_train()
                            self.training = True
                            dpg.configure_item("_button_train", label="stop")

                    # dpg.add_button(
                    #     label="init", tag="_button_init", callback=self.prepare_train
                    # )
                    # dpg.bind_item_theme("_button_init", theme_button)

                    dpg.add_button(
                        label="start", tag="_button_train", callback=callback_train
                    )

                    # restart
                    def callback_restart(sender, app_data):
                        self.renderer.initialize(num_pts=self.opt.num_pts)
                        self.prepare_train()
                        self.training = False
                        dpg.configure_item("_button_train", label="start")

                    dpg.add_button(label="restart", callback=callback_restart)

                    dpg.bind_item_theme("_button_train", theme_button)

                with dpg.group(horizontal=True):
                    dpg.add_text("", tag="_log_train_time")
                    dpg.add_text("", tag="_log_train_log")

            # rendering options
            with dpg.collapsing_header(label="Rendering", default_open=True):
                # mode combo
                def callback_change_mode(sender, app_data):
                    self.mode = app_data
                    self.need_update = True

                dpg.add_combo(
                    ("image", "depth", "alpha"),
                    label="mode",
                    default_value=self.mode,
                    callback=callback_change_mode,
                )

                # fov slider
                def callback_set_fovy(sender, app_data):
                    self.cam.fovy = np.deg2rad(app_data)
                    self.need_update = True

                dpg.add_slider_int(
                    label="FoV (vertical)",
                    min_value=1,
                    max_value=120,
                    format="%d deg",
                    default_value=np.rad2deg(self.cam.fovy),
                    callback=callback_set_fovy,
                )

                def callback_set_gaussain_scale(sender, app_data):
                    self.gaussain_scale_factor = app_data
                    self.need_update = True

                dpg.add_slider_float(
                    label="gaussain scale",
                    min_value=0,
                    max_value=1,
                    format="%.2f",
                    default_value=self.gaussain_scale_factor,
                    callback=callback_set_gaussain_scale,
                )

        # register camera handler

        def callback_camera_drag_rotate_or_draw_mask(sender, app_data):
            if not dpg.is_item_focused("_primary_window"):
                return

            dx = app_data[1]
            dy = app_data[2]

            self.cam.orbit(dx, dy)
            self.need_update = True

        def callback_camera_wheel_scale(sender, app_data):
            if not dpg.is_item_focused("_primary_window"):
                return

            delta = app_data

            self.cam.scale(delta)
            self.need_update = True

        def callback_camera_drag_pan(sender, app_data):
            if not dpg.is_item_focused("_primary_window"):
                return

            dx = app_data[1]
            dy = app_data[2]

            self.cam.pan(dx, dy)
            self.need_update = True

        def callback_set_mouse_loc(sender, app_data):
            if not dpg.is_item_focused("_primary_window"):
                return

            # just the pixel coordinate in image
            self.mouse_loc = np.array(app_data)

        with dpg.handler_registry():
            # for camera moving
            dpg.add_mouse_drag_handler(
                button=dpg.mvMouseButton_Left,
                callback=callback_camera_drag_rotate_or_draw_mask,
            )
            dpg.add_mouse_wheel_handler(callback=callback_camera_wheel_scale)
            dpg.add_mouse_drag_handler(
                button=dpg.mvMouseButton_Middle, callback=callback_camera_drag_pan
            )

        dpg.create_viewport(
            title="Gaussian3D",
            width=self.W + 600,
            height=self.H + (45 if os.name == "nt" else 0),
            resizable=False,
        )

        # global theme
        with dpg.theme() as theme_no_padding:
            with dpg.theme_component(dpg.mvAll):
                # set all padding to 0 to avoid scroll bar
                dpg.add_theme_style(
                    dpg.mvStyleVar_WindowPadding, 0, 0, category=dpg.mvThemeCat_Core
                )
                dpg.add_theme_style(
                    dpg.mvStyleVar_FramePadding, 0, 0, category=dpg.mvThemeCat_Core
                )
                dpg.add_theme_style(
                    dpg.mvStyleVar_CellPadding, 0, 0, category=dpg.mvThemeCat_Core
                )

        dpg.bind_item_theme("_primary_window", theme_no_padding)

        dpg.setup_dearpygui()

        # register a larger font
        # get it from: https://github.com/lxgw/LxgwWenKai/releases/download/v1.300/LXGWWenKai-Regular.ttf
        if os.path.exists("LXGWWenKai-Regular.ttf"):
            with dpg.font_registry():
                with dpg.font("LXGWWenKai-Regular.ttf", 18) as default_font:
                    dpg.bind_font(default_font)

        # dpg.show_metrics()

        dpg.show_viewport()

    def render(self):
        assert self.gui
        while dpg.is_dearpygui_running():
            # update texture every frame
            if self.training:
                self.train_step()
            self.test_step()
            dpg.render_dearpygui_frame()

    # no gui mode
    def train(self, iters=500):
        if iters > 0:
            self.prepare_train()
            for i in tqdm.trange(iters):
                self.train_step()
            # do a last prune
            self.renderer.gaussians.prune(
                min_opacity=0.01, extent=1, max_screen_size=1)
        # save
        self.save_model(mode='model')
        self.save_model(mode='geo+tex')


if __name__ == "__main__":
    import argparse
    from omegaconf import OmegaConf

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True,
                        help="path to the yaml config file")
    args, extras = parser.parse_known_args()

    # override default config from cli
    opt = OmegaConf.merge(OmegaConf.load(args.config),
                          OmegaConf.from_cli(extras))

    gui = GUI(opt)

    if opt.gui:
        gui.render()
    else:
        gui.train(opt.iters)