"""Replace the "Last Saved By" name PowerPoint wrote into a .ppt (#957).

PowerPoint for Mac fills the SummaryInformation property "Last Saved
By" from the signed-in account on every save, whatever the AppleScript
sets, so ``generate-ppt.sh`` runs this after saving. It overwrites
every copy of that name in the SummaryInformation stream with a
synthetic name of the same length, so no offset or stream size moves.
The slide records (the "PowerPoint Document" stream) are not touched.

Usage: python -I scrub-ppt-author.py <file.ppt>  (needs olefile)
"""

import sys

import olefile

_STREAM = "\x05SummaryInformation"
_SYNTHETIC = b"Synthetic Author"


def main(path: str) -> None:
    with olefile.OleFileIO(path, write_mode=True) as ole:
        name = ole.get_metadata().last_saved_by
        if not name or name.startswith(_SYNTHETIC[: len(name)]):
            print("Last Saved By is already synthetic or empty")
            return
        replacement = _SYNTHETIC.ljust(len(name))[: len(name)]
        data = ole.openstream(_STREAM).read()
        ole.write_stream(_STREAM, data.replace(name, replacement))
    print("replaced Last Saved By with a synthetic name")


if __name__ == "__main__":
    main(sys.argv[1])
