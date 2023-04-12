#!/usr/bin/env python3

import argparse
import gc
import shutil
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
from IPython import embed
from matplotlib.patches import Rectangle

# from torch.utils.data import DataLoader, TensorDataset
from scipy.signal import find_peaks

from models.modelhandling import load_model
from utils.datahandling import find_on_time, resize_image
from utils.filehandling import ConfLoader, NumpyLoader
from utils.logger import make_logger
from utils.plotstyle import PlotStyle

# import matplotlib
# matplotlib.use('Agg')

logger = make_logger(__name__)
conf = ConfLoader("config.yml")
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
ps = PlotStyle()


class Detector:
    def __init__(self, modelpath, dataset, mode):
        assert mode in [
            "memory",
            "disk",
        ], "Mode must be either 'memory' or 'disk'"
        logger.info("Initializing detector...")

        self.mode = mode
        self.model = load_model(modelpath)
        self.data = dataset
        self.samplerate = conf.samplerate
        self.fill_samplerate = 1 / np.mean(np.diff(self.data.fill_times))
        self.freq_pad = conf.freq_pad
        self.time_pad = conf.time_pad
        self.window_size = int(conf.time_pad * 2 * self.fill_samplerate)
        self.stride = int(conf.stride * self.fill_samplerate)
        self.detected_chirps = None
        self.detected_chirp_ids = None

        if (self.data.times[-1] // 600 != 0) and (self.mode == "memory"):
            logger.warning(
                "It is recommended to process recordings longer than 10 minutes using the 'disk' mode"
            )

        if self.window_size % 2 == 0:
            self.window_size += 1
            logger.info(f"Time padding is not odd. Adding one.")

        if self.stride % 2 == 0:
            self.stride += 1
            logger.info(f"Stride is not odd. Adding one.")

    def classify_single(self, img):
        with torch.no_grad():
            img = torch.from_numpy(img).to(device)
            outputs = self.model(img)
            probs = F.softmax(outputs, dim=1)
            _, preds = torch.max(outputs, dim=1)
        probs = probs.cpu().numpy()[0][0]
        preds = preds.cpu().numpy()[0]
        return probs, preds

    def detect(self, plot=False):
        logger.info("Detecting...")
        if self.mode == "memory":
            self._detect_memory(plot)
        else:
            self._detect_disk(plot)

    def _detect_memory(self, plot):
        logger.info("Processing in memory...")

        first_index = 0
        last_index = self.data.fill_times.shape[0]
        window_start_indices = np.arange(
            first_index, last_index - self.window_size, self.stride, dtype=int
        )

        detected_chirps = []
        detected_chirp_probs = []
        detected_chirp_ids = []

        iter = 0
        for track_id in np.unique(self.data.ident_v):
            logger.info(f"Processing track {track_id}...")
            track = self.data.fund_v[self.data.ident_v == track_id]
            time = self.data.times[
                self.data.idx_v[self.data.ident_v == track_id]
            ]

            predicted_labels = []
            predicted_probs = []
            center_t = []

            for window_start_index in window_start_indices:
                # Make index were current window will end
                window_end_index = window_start_index + self.window_size

                # Get the current frequency from the track
                center_idx = int(
                    window_start_index + np.floor(self.window_size / 2) + 1
                )
                window_center_t = self.data.fill_times[center_idx]
                track_index = find_on_time(time, window_center_t)
                center_freq = track[track_index]

                # From the track frequency compute the frequency
                # boundaries

                freq_min = center_freq + self.freq_pad[0]
                freq_max = center_freq + self.freq_pad[1]

                # Find these values on the frequency axis of the spectrogram
                freq_min_index = find_on_time(self.data.fill_freqs, freq_min)
                freq_max_index = find_on_time(self.data.fill_freqs, freq_max)

                # Using window start, stop and feeq lims, extract snippet from spec
                snippet = self.data.fill_spec[
                    freq_min_index:freq_max_index,
                    window_start_index:window_end_index,
                ]
                snippet = (snippet - np.min(snippet)) / (
                    np.max(snippet) - np.min(snippet)
                )
                snippet = resize_image(snippet, conf.img_size_px)
                snippet = np.expand_dims(snippet, axis=0)
                snippet = np.asarray([snippet]).astype(np.float32)
                prob, label = self.classify_single(snippet)

                # Append snippet to list
                predicted_labels.append(label)
                predicted_probs.append(prob)
                center_t.append(window_center_t)

                iter += 1
                if not plot:
                    continue

                fig, ax = plt.subplots(1, 1, figsize=(24 * ps.cm, 12 * ps.cm))
                ax.imshow(
                    self.data.fill_spec,
                    aspect="auto",
                    origin="lower",
                    extent=[
                        self.data.fill_times[0],
                        self.data.fill_times[-1],
                        self.data.fill_freqs[0],
                        self.data.fill_freqs[-1],
                    ],
                    cmap="magma",
                    vmin=np.min(self.data.fill_spec) * 0.6,
                    vmax=np.max(self.data.fill_spec),
                    zorder=-100,
                    interpolation="gaussian",
                )
                # Create a Rectangle patch
                startx = self.data.fill_times[window_start_index]
                stopx = self.data.fill_times[window_end_index]
                starty = self.data.fill_freqs[freq_min_index]
                stopy = self.data.fill_freqs[freq_max_index]

                if label == 1:
                    patchc = ps.white
                else:
                    patchc = ps.maroon

                rect = Rectangle(
                    (startx, starty),
                    self.data.fill_times[window_end_index]
                    - self.data.fill_times[window_start_index],
                    self.data.fill_freqs[freq_max_index]
                    - self.data.fill_freqs[freq_min_index],
                    linewidth=2,
                    facecolor="none",
                    edgecolor=patchc,
                )

                # Add the patch to the Axes
                ax.add_patch(rect)

                # Add the chirpprob
                ax.text(
                    (startx + stopx) / 2,
                    stopy + 50,
                    f"{prob:.2f}",
                    color=ps.white,
                    fontsize=14,
                    horizontalalignment="center",
                    verticalalignment="center",
                )

                # Plot the track
                ax.plot(self.data.times, track, linewidth=1, color=ps.black)

                # Plot the window center
                ax.plot(
                    [
                        self.data.fill_times[
                            window_start_index + self.window_size // 2
                        ]
                    ],
                    [center_freq],
                    marker="o",
                    color=ps.black,
                )

                # make limits nice
                ax.set_ylim(np.min(track) - 200, np.max(track) + 400)
                startxw = startx - 5
                stopxw = stopx + 5

                if startxw < 0:
                    stopxw = stopxw - startxw
                    startxw = 0
                if stopxw > self.data.fill_times[-1]:
                    startxw = startxw - (stopxw - self.data.fill_times[-1])
                    stopxw = self.data.fill_times[-1]
                ax.set_xlim(startxw, stopxw)
                ax.axis("off")
                plt.subplots_adjust(left=-0.01, right=1, top=1, bottom=0)
                plt.savefig(f"../anim/test_{iter-1}.png")

                # Clear the plot
                plt.cla()
                plt.clf()
                plt.close("all")
                plt.close(fig)
                gc.collect()

            predicted_labels = np.asarray(predicted_labels)
            predicted_probs = np.asarray(predicted_probs)
            center_t = np.asarray(center_t)

            # detect the peaks in the probabilities
            # peaks of probabilities are chirps
            peaks, _ = find_peaks(predicted_probs, height=0.5)
            peaktimes = center_t[peaks]
            peakprobs = predicted_probs[peaks]

            if len(np.unique(peaks)) > 1:
                detfrac = len(peaks) / conf.num_chirps
                logger.info(f"Found {len(peaks)} chirps")

            detected_chirps.append(peaktimes)
            detected_chirp_probs.append(peakprobs)
            detected_chirp_ids.append(np.repeat(int(track_id), len(peaktimes)))

        self.detected_chirps = np.concatenate(detected_chirps)
        self.detected_chirp_probs = np.concatenate(detected_chirp_probs)
        self.detected_chirp_ids = np.concatenate(detected_chirp_ids)

    def _detect_disk(self, plot):
        logger.info("This function is not yet implemented. Aborting ...")

    def plot(self):
        d = self.data  # <----- Quick fix, remove this!!!
        # correct_chirps = np.load(
        #     conf.testing_data_path + "/correct_chirp_times.npy"
        # )
        # correct_chirp_ids = np.load(
        #     conf.testing_data_path + "/correct_chirp_time_ids.npy"
        # )

        fig, ax = plt.subplots(
            figsize=(24 * ps.cm, 12 * ps.cm), constrained_layout=True
        )
        ax.imshow(
            d.fill_spec,
            aspect="auto",
            origin="lower",
            extent=[
                d.fill_times[0],
                d.fill_times[-1],
                d.fill_freqs[0],
                d.fill_freqs[-1],
            ],
            zorder=-20,
            # vmin=np.min(d.fill_spec) * 0.6,
            # vmax=np.max(d.fill_spec),
            interpolation="gaussian",
        )

        for track_id in np.unique(d.ident_v):
            track_id = int(track_id)
            track = d.fund_v[d.ident_v == track_id]
            time = d.times[d.idx_v[d.ident_v == track_id]]
            freq = np.median(track)

            # correct_t = correct_chirps[correct_chirp_ids == track_id]
            # findex = np.asarray([find_on_time(d.times, t) for t in correct_t])
            # correct_f = track[findex]

            detect_t = self.detected_chirps[self.detected_chirp_ids == track_id]
            findex = np.asarray([find_on_time(d.times, t) for t in detect_t])
            detect_f = track[findex]

            ax.plot(time, track, linewidth=1, zorder=-10, color=ps.black)
            # ax.scatter(
            #     correct_t, correct_f, s=20, marker="o", color=ps.black, zorder=0
            # )
            ax.scatter(
                detect_t,
                detect_f,
                s=20,
                marker="o",
                color=ps.black,
                edgecolor=ps.black,
                zorder=10,
            )

        ax.set_ylim(np.min(d.fund_v - 100), np.max(d.fund_v + 300))
        ax.set_xlim(np.min(d.fill_times), np.max(d.fill_times))
        ax.set_xlabel("Time [s]")
        ax.set_ylabel("Frequency [Hz]")

        plt.savefig("../assets/detection.png")
        plt.show()


def interface():
    parser = argparse.ArgumentParser(
        description="Detects chirps on spectrograms."
    )
    parser.add_argument(
        "--path",
        type=str,
        default=conf.testing_data_path,
        help="Path to the dataset to use for detection",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="memory",
        help="Mode to use for detection. Can be either 'memory' or 'disk'. Defaults to 'memory'.",
    )
    args = parser.parse_args()
    return args


def main():
    args = interface()
    d = NumpyLoader(args.path)
    modelpath = conf.save_dir
    det = Detector(modelpath, d, args.mode)
    det.detect(plot=False)
    det.plot()


if __name__ == "__main__":
    main()
