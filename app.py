"""Startpunt voor de Mac-app (PyInstaller bouwt vanaf dit bestand).

Zonder argumenten opent het venster; met argumenten (bv. --list) werkt
dezelfde binary als command line-tool.
"""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-psn"):  # -psn_… geeft macOS soms mee
        from open_fujitransfer.cli import main as cli_main
        sys.exit(cli_main())
    from open_fujitransfer.gui import main
    main()
