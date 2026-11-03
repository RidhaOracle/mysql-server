# Copyright (c) 2026, Oracle and/or its affiliates.
from .pipeline import main
from .policy import Blocked

if __name__ == '__main__':
    try:
        main()
    except Blocked as error:
        raise SystemExit(str(error)) from None
