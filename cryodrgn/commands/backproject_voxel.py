"""
Backproject cryo-EM images
"""

import argparse
import os
import time
import math
import numpy as np
import torch
import logging
from cryodrgn import ctf, dataset, fft, utils
from cryodrgn.mrc import MRCFile
from cryodrgn.lattice import Lattice
from cryodrgn.pose import PoseTracker

logger = logging.getLogger(__name__)


def add_args(parser):
    parser.add_argument(
        "particles",
        type=os.path.abspath,
        help="Input particles (.mrcs, .star, .cs, or .txt)",
    )
    parser.add_argument(
        "--poses", type=os.path.abspath, required=True, help="Image poses (.pkl)"
    )
    parser.add_argument(
        "--ctf",
        metavar="pkl",
        type=os.path.abspath,
        help="CTF parameters (.pkl) for phase flipping images",
    )
    parser.add_argument(
        "-o", type=os.path.abspath, required=True, help="Output .mrc file"
    )

    group = parser.add_argument_group("Dataset loading options")
    group.add_argument(
        "--uninvert-data",
        dest="invert_data",
        action="store_false",
        help="Do not invert data sign",
    )
    group.add_argument(
        "--datadir",
        type=os.path.abspath,
        help="Path prefix to particle stack if loading relative paths from a .star or .cs file",
    )
    group.add_argument(
        "--lazy",
        action="store_true",
        help="Lazy loading if full dataset is too large to fit in memory",
    )
    group.add_argument("--ind", help="Indices to iterate over (pkl)")
    group.add_argument(
        "--first",
        type=int,
        default=10000,
        help="Backproject the first N images (default: %(default)s)",
    )
    group = parser.add_argument_group("Tilt series options")
    group.add_argument("--tilt", help="Tilt series .mrcs image stack")
    group.add_argument(
        "--tilt-deg",
        type=float,
        default=45,
        help="Right-handed x-axis tilt offset in degrees (default: %(default)s)",
    )
    group.add_argument(
        "--do-tilt-series", 
        dest="do_tilt_series", 
        action="store_true", 
        help="Store data as tilt series"
    )
    group.add_argument(
        "--ntilts",
        type=int,
        default=10,
        help="Number of tilts to encode (default: %(default)s)",
    )
    group.add_argument(
        "--voltage", 
        type=int,
        default=300,
        help="Microscope voltage (default: %(default)s)"
    )
    group.add_argument(
        "--dose_per_tilt", 
        type=float,
        default=2.93,
        help="Expected dose per tilt (electrons/A^2 per tilt) (default: %(default)s)"
    )
    group.add_argument(
        "--angle_per_tilt", 
        type=float,
        default=3,
        help="Tilt angle increment per tilt in degrees (default: %(default)s)"
    )

    return parser


def add_slice(V, counts, ff_coord, ff, D):
    d2 = int(D / 2)
    ff_coord = ff_coord.transpose(0, 1)
    xf, yf, zf = ff_coord.floor().long()
    xc, yc, zc = ff_coord.ceil().long()

    def add_for_corner(xi, yi, zi):
        dist = torch.stack([xi, yi, zi]).float() - ff_coord
        w = 1 - dist.pow(2).sum(0).pow(0.5)
        w[w < 0] = 0
        V[(zi + d2, yi + d2, xi + d2)] += w * ff
        counts[(zi + d2, yi + d2, xi + d2)] += w

    add_for_corner(xf, yf, zf)
    add_for_corner(xc, yf, zf)
    add_for_corner(xf, yc, zf)
    add_for_corner(xf, yf, zc)
    add_for_corner(xc, yc, zf)
    add_for_corner(xf, yc, zc)
    add_for_corner(xc, yf, zc)
    add_for_corner(xc, yc, zc)
    return V, counts


def main(args):
    assert args.o.endswith(".mrc")

    t1 = time.time()
    logger.info(args)
    if not os.path.exists(os.path.dirname(args.o)):
        os.makedirs(os.path.dirname(args.o))

    # set the device
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    logger.info("Use cuda {}".format(use_cuda))
    if not use_cuda:
        logger.warning("WARNING: No GPUs detected")

    # load the particles
    if args.ind is not None:
        args.ind = utils.load_pkl(args.ind).astype(int)
    if args.tilt is None:
        tilt = None
    else:  # tilt series
        tilt = torch.tensor(utils.xrot(args.tilt_deg).astype(np.float32), device=device)

    if args.do_tilt_series:
        data = dataset.TiltSeriesData(
            args.particles, 
            args.ntilts,
            norm=(0, 1),
            invert_data=args.invert_data, 
            datadir=args.datadir,
            ind=args.ind,
            lazy=args.lazy,
            voltage=args.voltage,
            dose_per_tilt=args.dose_per_tilt,
            angle_per_tilt=args.angle_per_tilt
        )
    else:
        data = dataset.ImageDataset(
            mrcfile=args.particles,
            norm=(0, 1),
            invert_data=args.invert_data,
            datadir=args.datadir,
            ind=args.ind,
            lazy=args.lazy,
        )

    D = data.D
    Nimg = data.N
    
    lattice = Lattice(D, extent=D // 2, device=device)

    posetracker = PoseTracker.load(args.poses, Nimg, D, None, args.ind, device=device)

    if args.ctf is not None:
        logger.info("Loading ctf params from {}".format(args.ctf))
        ctf_params = ctf.load_ctf_for_training(D - 1, args.ctf)
        if args.ind is not None:
            ctf_params = ctf_params[args.ind]
        if args.do_tilt_series:
            ctf_params = np.concatenate(
                (ctf_params, data.ctfscalefactor.reshape(-1, 1)), axis=1  # type: ignore
            )
        ctf_params = torch.tensor(ctf_params, device=device)
    else:
        ctf_params = None
    Apix = float(ctf_params[0, 0]) if ctf_params is not None else 1.0

    V = torch.zeros((D, D, D), device=device)
    counts = torch.zeros((D, D, D), device=device)

    mask = lattice.get_circular_mask(D // 2)

    if args.first:
        args.first = min(args.first, Nimg)
        iterator = range(args.first)
    else:
        iterator = range(Nimg)

    for ii in iterator:
        if ii % 100 == 0:
            logger.info("image {}".format(ii))
        r, t = posetracker.get_pose(ii)
        ff = data.get_tilt(index) if args.do_tilt_series else data[ii]
        assert isinstance(ff, tuple)

        if tilt is not None:
            assert isinstance(ff, tuple)
            ff_tilt = ff[1]
        else:
            ff_tilt = None
        ff = ff[0].to(device)
        ff = ff.view(-1)[mask]
        c = None
        if ctf_params is not None:
            freqs = lattice.freqs2d / ctf_params[ii, 0]
            c = ctf.compute_ctf(freqs, *ctf_params[ii, 1:]).view(-1)[mask]
            ff *= c.sign()
        if t is not None:
            ff = lattice.translate_ht(ff.view(1, -1), t.view(1, 1, 2), mask).view(-1)
        if args.do_tilt_series:
            freqs = lattice.freqs2d / ctf_params[ii, 0]
            x = freqs[..., 0]
            y = freqs[..., 1]
            s2 = x**2 + y**2
            cumulative_dose = data.tilt_numbers[ii] * data.dose_per_tilt
            exp_correction = torch.exp(-cumulative_dose/data.critical_exposure(torch.sqrt(s2)))
            ff = torch.mul(ff, torch.from_numpy(exp_correction).to(device))
            ff = torch.mul(ff, math.cos(data.tilt_angles[ii] * np.pi/180))

        ff_coord = lattice.coords[mask] @ r
        add_slice(V, counts, ff_coord, ff, D)

        # tilt series
        if ff_tilt is not None:
            ff_tilt.to(device)
            ff_tilt = ff_tilt.view(-1)[mask]
            if c is not None:
                ff_tilt *= c.sign()
            if t is not None:
                ff_tilt = lattice.translate_ht(
                    ff_tilt.view(1, -1), t.view(1, 1, 2), mask
                ).view(-1)
            ff_coord = lattice.coords[mask] @ tilt @ r
            add_slice(V, counts, ff_coord, ff_tilt, D)

    td = time.time() - t1
    logger.info(
        "Backprojected {} images in {}s ({}s per image)".format(
            len(iterator), td, td / Nimg
        )
    )
    counts[counts == 0] = 1
    V /= counts
    V = fft.ihtn_center(V[0:-1, 0:-1, 0:-1].cpu())
    MRCFile.write(args.o, np.array(V).astype("float32"), Apix=Apix)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    main(add_args(parser).parse_args())
