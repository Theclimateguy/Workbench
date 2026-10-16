#!/usr/bin/env python3
"""Render the theme overlay images into static/frames/ — original procedural art in the spirit of the StarCraft
consoles (Terran: riveted gunmetal plates with teal glass; Zerg: chitin carapace, bone teeth, glowing veins).

    python art/frames.py        # needs numpy + pillow; the output is committed, the box never runs this

Per theme:
  <theme>-frame.png   heavy 9-slice frame (composer console, top bar and sidebar edges) — CSS border-image
  <theme>-panel.png   light 9-slice frame (menus, modals, cards)
  <theme>-bg.png      seamless background tile
All images are 2x; CSS uses half the pixel sizes. Edge slices tile seamlessly (`border-image-repeat: round`):
every texture is sampled from a torus whose period equals the edge slice length.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path(__file__).resolve().parent.parent / "static" / "frames"


# ---------- primitives ----------

def noise(shape, beta=1.6, seed=0, aniso=(1.0, 1.0)):
    """Periodic fractal noise in [-1, 1] (1/f^beta spectrum). aniso stretches it along y or x (brushed metal)."""
    rng = np.random.default_rng(seed)
    h, w = shape
    fy = np.fft.fftfreq(h)[:, None] * aniso[0]
    fx = np.fft.fftfreq(w)[None, :] * aniso[1]
    f = np.sqrt(fx ** 2 + fy ** 2)
    f[0, 0] = 1
    spec = np.fft.fft2(rng.standard_normal(shape)) / f ** beta
    spec[0, 0] = 0
    n = np.real(np.fft.ifft2(spec))
    return n / np.abs(n).max()


def blur(a, sigma):
    """Periodic gaussian blur via FFT."""
    h, w = a.shape
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    g = np.exp(-2 * (np.pi * sigma) ** 2 * (fx ** 2 + fy ** 2))
    return np.real(np.fft.ifft2(np.fft.fft2(a) * g))


def smooth(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def shade(height, scale, base, gloss, spec_col, light=(-0.45, -0.65, 0.62), power=36):
    gy, gx = np.gradient(height * scale)
    n = np.dstack([-gx, -gy, np.ones_like(height)])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    l = np.array(light) / np.linalg.norm(light)
    hv = (l + [0, 0, 1]) / np.linalg.norm(l + [0, 0, 1])
    diff = np.clip(n @ l, 0, 1)[..., None]
    spec = (np.clip(n @ hv, 0, 1) ** power)[..., None] * gloss[..., None]
    return base * (0.28 + 0.95 * diff) + spec * np.array(spec_col)


def lerp3(a, b, t):
    return np.array(a) * (1 - t[..., None]) + np.array(b) * t[..., None]


def save(rgb, alpha, name):
    OUT.mkdir(parents=True, exist_ok=True)
    img = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha, 0, 1) * 255]).astype(np.uint8)
    Image.fromarray(img, "RGBA").save(OUT / name, optimize=True)
    print(f"{name}: {img.shape[1]}x{img.shape[0]}, {(OUT / name).stat().st_size // 1024} KB")


class Frame:
    """Geometry of a square 9-slice frame: W px, slice s, ring thickness t, outer margin m (room for teeth)."""

    def __init__(self, W, s, t, m, R):
        self.W, self.s, self.t, self.m, self.P = W, s, t, m, W - 2 * s
        Y, X = np.mgrid[0:W, 0:W].astype(float)
        self.X, self.Y = X, Y
        c, half = (W - 1) / 2, (W - 1) / 2 - m
        qx, qy = np.abs(X - c) - (half - R), np.abs(Y - c) - (half - R)
        outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - R
        self.d = -outside                                  # px inside the outer edge
        self.horiz = np.minimum(Y, W - 1 - Y) < np.minimum(X, W - 1 - X)
        self.along = np.where(self.horiz, X - s, Y - s)    # coordinate along the nearest edge (period P)
        self.corner = ((X < s) | (X >= W - s)) & ((Y < s) | (Y >= W - s))
        # where the rounded corner's ring midline crosses the diagonal, per corner
        k = R - (R - t / 2) / np.sqrt(2) + m
        self.knots = [(k, k), (W - 1 - k, k), (k, W - 1 - k), (W - 1 - k, W - 1 - k)]

    def tex(self, torus):
        """Sample a P x P periodic texture so that edges tile."""
        P = self.P
        return torus[((self.Y - self.s) % P).astype(int), ((self.X - self.s) % P).astype(int)]

    def knot_dist(self):
        return np.min([np.hypot(self.X - x, self.Y - y) for x, y in self.knots], axis=0)


# ---------- Zerg: chitin carapace ----------

def zerg_frame(W, s, t, m, R, seed, ribs, spikes, nodule, name):
    F = Frame(W, s, t, m, R)
    P = F.P
    n1 = F.tex(noise((P, P), 1.8, seed))
    n2 = F.tex(noise((P, P), 1.1, seed + 1))
    edge = F.tex(noise((P, P), 2.3, seed + 2))
    pulse = F.tex(blur(noise((P, P), 1.4, seed + 3), 2))           # brightness along the membrane
    a = 2 * np.pi * F.along / P
    d = F.d + edge * t * 0.07
    u = np.clip(d / t, 0, 1)

    # bone spikes: pointed, only on the outer edge, `spikes` per tile, none in the corners
    if spikes:
        ph = ((F.along / P * spikes) % 1) - 0.5                      # -0.5..0.5 around each spike
        spike_len = m * 0.95 * np.clip(1 - np.abs(ph) / 0.11, 0, 1) ** 1.3 * ~F.corner
    else:
        spike_len = np.zeros_like(d)
    spike = (d < 0.5) & (-d < spike_len)                             # pixels outside the ring, inside a spike

    kd = F.knot_dist()
    rn = t * nodule
    knob = np.clip(1 - (kd / rn) ** 2, 0, 1) if nodule else np.zeros_like(d)

    ring = smooth(-1.2, 1.2, d) * (1 - smooth(t - 1.5, t + 1.5, d))
    alpha = np.maximum.reduce([ring, spike * 1.0, smooth(0, 0.08, knob)])
    inner = d - t
    alpha = np.maximum(alpha, (inner > 0) * (knob == 0) * 0.65 * np.clip(1 - inner / (t * 0.5), 0, 1) ** 2)

    # carapace: two rounded plates with a membrane channel between them; segment ridges across the plates
    plates = np.sin(np.pi * u) ** 0.5 * (1 - 0.9 * np.exp(-((u - 0.5) / 0.09) ** 2))
    rib = np.maximum(0, np.cos(a * ribs)) ** 3 * 0.25 * np.sin(np.pi * u) * ~F.corner if ribs else 0
    h = plates + 0.16 * n1 + 0.05 * n2 + rib
    h = np.where(spike, (spike_len + d) / np.maximum(spike_len, 1e-3) * 0.9, h)
    h = np.maximum(h * ((ring > 0.01) | spike), knob ** 0.55 * 1.2)
    channel = np.exp(-((u - 0.5) / 0.07) ** 2) * (ring > 0.5) * (knob < 0.05)

    t_col = np.clip(0.45 + 0.45 * n1 + 0.35 * (h - 0.7), 0, 1)
    base = lerp3((12, 6, 10), (58, 32, 42), t_col)
    bone = np.clip(spike * 1.0 + smooth(0.95, 1.25, h) * 0.35 * (knob == 0), 0, 1)
    base = lerp3(base, (150, 128, 100), bone)
    gloss = 0.45 + 0.35 * (n2 > 0.1)
    rgb = shade(h, t * 0.5, base, gloss, (255, 206, 170))
    glow = (0.55 + 0.45 * pulse)[..., None]
    rgb += channel[..., None] * glow * np.array([255, 120, 18]) * 0.85
    rgb += (np.exp(-((u - 0.5) / 0.16) ** 2) * (ring > 0.5) * (knob < 0.05))[..., None] * glow * np.array([90, 30, 0]) * 0.45
    if nodule:  # dark socket with a slit of light: the hive watching
        X, Y = F.X, F.Y
        eye = np.zeros_like(d)
        for kx, ky in F.knots:
            dx, dy = (X - kx) / (rn * 0.55), (Y - ky) / (rn * 0.2)
            eye = np.maximum(eye, np.clip(1 - (dx ** 2 + dy ** 2), 0, 1))
        socket = np.clip(1 - kd / (rn * 0.62), 0, 1)
        rgb = rgb * (1 - 0.8 * socket[..., None] ** 0.5)
        rgb += (eye ** 0.8)[..., None] * np.array([255, 150, 30]) + (eye ** 0.3)[..., None] * np.array([90, 30, 0]) * 0.5
        pupil = np.zeros_like(d)
        for kx, ky in F.knots:
            pupil = np.maximum(pupil, np.clip(1 - (((X - kx) / (rn * 0.07)) ** 2 + ((Y - ky) / (rn * 0.19)) ** 2), 0, 1))
        rgb = rgb * (1 - smooth(0, 0.3, pupil)[..., None] * 0.92)
    rgb = np.where(((inner > 0) & (knob == 0) & (ring < 0.5))[..., None], 0, rgb)
    save(rgb, alpha, name)


def zerg_bg(N, seed, name):
    n1, n2 = noise((N, N), 2.0, seed), noise((N, N), 1.3, seed + 1)
    r = 1 - np.abs(noise((N, N), 1.7, seed + 2))
    vein = smooth(0.955, 0.99, r)
    h = 0.6 * n1 + 0.12 * n2 + 0.35 * blur(vein, 1.5)
    base = lerp3((5, 3, 5), (19, 9, 15), np.clip(0.5 + 0.6 * n1, 0, 1))
    rgb = shade(h, 34, base, np.full(n1.shape, 0.2), (80, 50, 64))
    rgb += vein[..., None] * np.array([34, 12, 18]) + blur(vein, 4)[..., None] * np.array([26, 8, 4])
    save(rgb, np.ones_like(n1), name)


# ---------- Terran: riveted gunmetal ----------

def terran_frame(W, s, t, m, R, seed, rivets, name):
    F = Frame(W, s, t, m, R)
    P = F.P
    brush_h = F.tex(noise((P, P), 1.4, seed, aniso=(9, 1)))   # streaks along x
    brush_v = F.tex(noise((P, P), 1.4, seed + 1, aniso=(1, 9)))
    brush = np.where(F.horiz, brush_h, brush_v)
    grime = F.tex(noise((P, P), 2.0, seed + 2))
    d = F.d
    u = d / t
    ring = smooth(-1, 1, d) * (1 - smooth(t - 1, t + 1, d))
    inner = d - t
    alpha = np.maximum(ring, (inner > 0) * 0.55 * np.clip(1 - inner / (t * 0.4), 0, 1) ** 2)

    # profile: outer bevel, raised band, groove, inner plate, inner bevel
    h = (smooth(0, 0.1, u) * 1.0 - smooth(0.52, 0.56, u) * 0.45 + smooth(0.6, 0.64, u) * 0.25
         - smooth(0.9, 1.0, u) * 0.8)
    a = F.along
    seam = (np.abs(((a + P / 2) % P) - P / 2) < 1.5) & ~F.corner & (u < 0.52)        # panel seam once per tile
    h -= seam * 0.35
    # rivets on the outer band: `rivets` per tile along each edge, plus one in each corner
    band_mid = t * 0.29
    pitch = P / rivets
    ax = ((a % pitch) - pitch / 2)
    rdist = np.hypot(ax, F.d - band_mid)
    rv = np.where(F.corner, F.knot_dist() * 1.0, rdist)
    rr = t * 0.085
    rivet = np.clip(1 - (rv / rr) ** 2, 0, 1)
    h += np.sqrt(rivet) * 0.35
    h += 0.02 * brush
    base = lerp3((26, 30, 36), (92, 100, 110), np.clip(0.55 + 0.25 * brush + 0.2 * grime, 0, 1))
    base = lerp3(base, (14, 16, 19), smooth(0.52, 0.6, u) * (1 - smooth(0.6, 0.66, u)))   # dark groove
    # hazard stripes on the inner plate, corners only
    hz = ((F.X + F.Y) // (t * 0.22) % 2 == 0) & F.corner & (u > 0.66) & (u < 0.88)
    base = np.where(hz[..., None], np.array([150, 112, 20]), base)
    gloss = 0.5 + 0.4 * np.clip(brush, 0, 1)
    rgb = shade(h, t * 0.6, base, gloss, (210, 230, 240), power=48)
    glass = np.exp(-((u - 0.955) / 0.022) ** 2) * (ring > 0.5)                    # teal light strip
    rgb += glass[..., None] * np.array([40, 190, 220]) * 0.9
    rgb = np.where(((inner > 0) & (ring < 0.5))[..., None], 0, rgb)
    save(rgb, alpha, name)


def terran_bg(N, seed, name):
    brush = noise((N, N), 1.3, seed, aniso=(7, 1))
    grime = noise((N, N), 2.0, seed + 1)
    Y, X = np.mgrid[0:N, 0:N].astype(float)
    cell = N / 4
    gx, gy = np.abs((X % cell) - cell / 2), np.abs((Y % cell) - cell / 2)
    edge = np.maximum(gx, gy) / (cell / 2)
    h = -smooth(0.965, 0.99, edge) * 1.0 + 0.03 * brush
    corner = np.hypot(cell / 2 - gx - 9, cell / 2 - gy - 9)
    h += np.sqrt(np.clip(1 - (corner / 4.5) ** 2, 0, 1)) * 0.5
    base = lerp3((8, 10, 13), (24, 28, 34), np.clip(0.5 + 0.3 * brush + 0.25 * grime, 0, 1))
    rgb = shade(h, 18, base, np.full(brush.shape, 0.3), (90, 110, 125))
    save(rgb, np.ones_like(brush), name)


if __name__ == "__main__":
    zerg_frame(512, 128, 52, 16, 46, seed=7, ribs=4, spikes=4, nodule=0.85, name="zerg-frame.png")
    zerg_frame(256, 64, 20, 6, 22, seed=17, ribs=2, spikes=0, nodule=0.0, name="zerg-panel.png")
    zerg_bg(512, 27, "zerg-bg.png")
    terran_frame(512, 128, 52, 4, 30, seed=5, rivets=3, name="terran-frame.png")
    terran_frame(256, 64, 20, 2, 12, seed=15, rivets=2, name="terran-panel.png")
    terran_bg(512, 25, "terran-bg.png")
