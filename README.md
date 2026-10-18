# Tonewright

A personal audio converter, waveform editor, multitrack mixer, tag editor and audiobook maker for **Windows** and for
**Macs with Apple Silicon**. It works offline, and your files are never changed unless you press Save.

![Editing](tonewright/docs/edit.png)

## Download

Get the latest version from the **[Releases](../../releases/latest)** page:

* **Windows 10 / 11:** `Tonewright-Setup-<version>.exe`. It installs into `C:\Program Files\Tonewright` (Windows asks
  for administrator approval once) and has English and Ukrainian setup screens.
* **Mac (M1 and newer):** `Tonewright-<version>-arm64.dmg`. The app is not notarised by Apple, so the first start needs
  one extra step, explained in the guide.

Each release lists SHA-256 checksums so you can check the download.

## Guide

Everything about using, building and developing the app is in **[tonewright/README.md](tonewright/README.md)**: the five
modes (Convert, Edit, Multitrack, Tags, Audiobook), the shortcuts, troubleshooting and how to build the installers.

GitHub builds and tests the Mac version on a real Apple Silicon Mac for every change (see the **Actions** tab).

## History

This repository once also held an earlier command-line toolkit (`audio_toolkit`) and a browser mixer prototype
(`layer-mixer`). Tonewright replaces both, and they were removed after version 1.1.0; they are still in the git history
(for example at commit `6ff530a`).
