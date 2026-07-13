# Cutting a release

The proven pipeline (first run: v1.2, 2026-07-11). Order matters: sign
before zipping, and never zip a staged folder (staging injects the local
`.env`).

1. **Bump** `APP_VERSION` in `config.py`. Installed apps compare release tags
   against it, and an unbumped version means nobody gets notified.
2. **Test + push**: `python -m pytest tests -q`, commit, push, wait for the
   GitHub check to go green.
3. **Build both flavors**:
   - `build_exe.bat` → `dist\<timestamp>\Plexxarr\` (folder pack). If Inno
     Setup (`ISCC.exe`) is installed, this also builds the installer at
     `packaging\Output\Plexxarr-<ver>-Setup.exe` (skipped silently if Inno
     Setup isn't present).
   - `python -m PyInstaller Plexxarr-portable.spec --noconfirm --distpath dist\portable`
4. **Sign** `Plexxarr.exe` (in the raw dist folder), `Plexxarr-portable.exe`,
   and `Plexxarr-<ver>-Setup.exe` per the private signing playbook: one
   `Invoke-TrustedSigning` call, paths comma-joined in a single `-Files`
   string. Verify all three: `Get-AuthenticodeSignature` → `Valid`,
   `CN=Charles Chambers`.
5. **Assemble the zip** from the RAW build folder (never from a staged one):
   bundle + `anime_meta.sqlite` + `.env.example` + `setup_autostart.bat` +
   `remove_autostart.bat` + `LICENSE` → `Plexxarr-<ver>-windows-x64.zip`.
6. **Sweep the zip**: list entries and fail on any `.env`, `*.db`, `*.pkl`,
   `*.pid`, or non-anime `.sqlite`.
7. **Tag + publish**: `git tag -a v<ver>`, push the tag, `gh release create`
   with the zip + portable exe + installer exe. GitHub attaches source
   zip/tar.gz itself.
8. **Emergency releases**: put a line starting `PLEXXARR-URGENT: <message>`
   in the release notes; installed apps show it as a red banner that
   ignores dismiss/mute settings.
9. **Locally**: `python stage_build.py` re-stages the new build with the
   local `.env`, and running `setup_autostart.bat` (elevated) repoints the
   autostart task at it.
