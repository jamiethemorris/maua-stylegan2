import uuid
import queue
import ffmpeg
import PIL.Image
import numpy as np
import torch as th
from threading import Thread

th.set_grad_enabled(False)
th.backends.cudnn.benchmark = True


def render(
    generator,
    latents,
    noise,
    offset,
    duration,
    batch_size,
    out_size,
    output_file,
    audio_file=None,
    truncation=1,
    bends=[],
    rewrites={},
    randomize_noise=False,
):
    split_queue = queue.Queue()
    render_queue = queue.Queue()

    # postprocesses batched torch tensors to individual RGB numpy arrays
    def split_batches(jobs_in, jobs_out):
        while True:
            try:
                imgs = jobs_in.get(timeout=10)
            except queue.Empty:
                return
            imgs = (imgs.clamp_(-1, 1) + 1) * 127.5
            imgs = imgs.permute(0, 2, 3, 1)
            for img in imgs:
                jobs_out.put(img.cpu().numpy().astype(np.uint8))
            jobs_in.task_done()

    # start background ffmpeg process that listens on stdin for frame data
    output_size = "1024x1024" if out_size == 1024 else ("512x512" if out_size == 512 else "1920x1080")
    if audio_file is not None:
        audio = ffmpeg.input(audio_file, ss=offset, to=offset + duration, guess_layout_max=0)
        video = (
            ffmpeg.input("pipe:", format="rawvideo", pix_fmt="rgb24", framerate=len(latents) / duration, s=output_size)
            .output(
                audio,
                output_file,
                framerate=len(latents) / duration,
                vcodec="libx264",
                preset="slow",
                audio_bitrate="320K",
                ac=2,
                v="warning",
            )
            .global_args("-hide_banner")
            .overwrite_output()
            .run_async(pipe_stdin=True)
        )
    else:
        video = (
            ffmpeg.input("pipe:", format="rawvideo", pix_fmt="rgb24", framerate=len(latents) / duration, s=output_size)
            .output(output_file, framerate=len(latents) / duration, vcodec="libx264", preset="slow", v="warning",)
            .global_args("-hide_banner")
            .overwrite_output()
            .run_async(pipe_stdin=True)
        )

    # writes numpy frames to ffmpeg stdin as raw rgb24 bytes
    def make_video(jobs_in):
        for _ in range(len(latents)):
            img = jobs_in.get(timeout=10)
            if img.shape[1] == 2048:
                img = img[:, 112:-112, :]
                im = PIL.Image.fromarray(img)
                img = np.array(im.resize((1920, 1080), PIL.Image.BILINEAR))
            assert (
                img.shape[1] == int(output_size.split("x")[1]) and img.shape[2] == int(output_size.split("x")[0]),
                "generator's output image size does not match specified output size",
            )
            video.stdin.write(img.tobytes())
            jobs_in.task_done()
        video.stdin.close()
        video.wait()

    splitter = Thread(target=split_batches, args=(split_queue, render_queue))
    splitter.daemon = True
    renderer = Thread(target=make_video, args=(render_queue,))
    renderer.daemon = True

    # make all data that needs to be loaded to the GPU float, contiguous, and pinned
    # the entire process is severly memory-transfer bound, but at least this might help a little
    latents = latents.float().contiguous().pin_memory()

    for ni, noise_scale in enumerate(noise):
        noise[ni] = noise_scale.float().contiguous().pin_memory() if noise_scale is not None else None

    param_dict = dict(generator.named_parameters())
    for param, (transform, modulation) in rewrites.items():
        rewrites[param] = [transform, modulation.float().contiguous().pin_memory()]
        original_weights[param] = param_dict[param].copy().cpu().float().contiguous().pin_memory()

    for bend in bends:
        if "modulation" in bend:
            bend["modulation"] = bend["modulation"].float().contiguous().pin_memory()

    if not isinstance(truncation, float):
        truncation = truncation.float().contiguous().pin_memory()

    for n in range(0, len(latents), batch_size):
        # load batches of data onto the GPU
        latent_batch = latents[n : n + batch_size].cuda(non_blocking=True)

        noise_batch = []
        for noise_scale in noise:
            if noise_scale is not None:
                noise_batch.append(noise_scale[n : n + batch_size].cuda(non_blocking=True))
            else:
                noise_batch.append(None)

        bend_batch = []
        if bends is not None:
            for bend in bends:
                if "modulation" in bend:
                    transform = bend["transform"](bend["modulation"][n : n + batch_size].cuda(non_blocking=True))
                    bend_batch.append({"layer": bend["layer"], "transform": transform})
                else:
                    bend_batch.append({"layer": bend["layer"], "transform": bend["transform"]})

        for param, rewrite in rewrites.items():
            rewritten_weight = rewrite(original_weights[param], n,).cuda(non_blocking=True)
            setattr(generator, param, th.nn.Parameter(rewritten_weight))

        truncation_batch = truncation[n : n + batch_size] if not isinstance(truncation, float) else truncation

        # forward through the generator
        outputs, _ = generator(
            styles=latent_batch,
            noise=noise_batch,
            truncation=truncation_batch,
            transform_dict_list=bend_batch,
            randomize_noise=randomize_noise,
            input_is_latent=True,
        )

        # send output to be split into frames and rendered one by one
        split_queue.put(outputs)

        if n == 0:
            splitter.start()
            renderer.start()

    splitter.join()
    renderer.join()
