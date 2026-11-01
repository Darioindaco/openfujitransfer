"""`python3 -m open_fujitransfer` start de app; met argumenten de command line."""
import sys

if len(sys.argv) > 1:
    from .cli import main
    sys.exit(main())
else:
    from .gui import main
    main()
