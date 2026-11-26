"""Center all independent Tk windows on the active screen."""

def center_window(widget):
    try:
        widget.update_idletasks()
        width=max(widget.winfo_width(),widget.winfo_reqwidth())
        height=max(widget.winfo_height(),widget.winfo_reqheight())
        screen_w=widget.winfo_screenwidth()
        screen_h=widget.winfo_screenheight()
        x=max(0,(screen_w-width)//2)
        y=max(0,(screen_h-height)//2)
        widget.geometry(f"+{x}+{y}")
    except Exception:
        pass
