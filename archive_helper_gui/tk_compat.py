from __future__ import annotations

from typing import Any, cast

try:
    from tkinter import (  # type: ignore
        BOTH as _TK_BOTH,
        END as _TK_END,
        LEFT as _TK_LEFT,
        RIGHT as _TK_RIGHT,
        X as _TK_X,
        BooleanVar as _TK_BooleanVar,
        IntVar as _TK_IntVar,
        Menu as _TK_Menu,
        StringVar as _TK_StringVar,
        Tk as _TK_Tk,
        Toplevel as _TK_Toplevel,
        filedialog as _TK_filedialog,
        messagebox as _TK_messagebox,
    )
    from tkinter import ttk as _TK_ttk  # type: ignore
    from tkinter.scrolledtext import ScrolledText as _TK_ScrolledText  # type: ignore

    BOTH = _TK_BOTH
    END = _TK_END
    LEFT = _TK_LEFT
    RIGHT = _TK_RIGHT
    X = _TK_X
    BooleanVar = _TK_BooleanVar
    IntVar = _TK_IntVar
    Menu = _TK_Menu
    StringVar = _TK_StringVar
    Tk = _TK_Tk
    Toplevel = _TK_Toplevel
    filedialog = _TK_filedialog
    messagebox = _TK_messagebox
    ttk = _TK_ttk
    ScrolledText = _TK_ScrolledText

    TK_AVAILABLE = True
except ModuleNotFoundError:
    TK_AVAILABLE = False

    # Define placeholders so references are always bound for type checking.
    # These are never used at runtime because the GUI exits early when TK_AVAILABLE is False.
    BOTH = cast(Any, "both")
    X = cast(Any, "x")
    LEFT = cast(Any, "left")
    RIGHT = cast(Any, "right")
    END = cast(Any, "end")

    StringVar = cast(Any, lambda *args, **kwargs: None)
    BooleanVar = cast(Any, lambda *args, **kwargs: None)
    IntVar = cast(Any, lambda *args, **kwargs: None)
    Menu = cast(Any, lambda *args, **kwargs: None)
    Tk = cast(Any, object)
    Toplevel = cast(Any, object)
    ttk = cast(Any, None)
    filedialog = cast(Any, None)
    messagebox = cast(Any, None)
    ScrolledText = cast(Any, object)
