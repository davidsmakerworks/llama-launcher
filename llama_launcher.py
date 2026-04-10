import wx
import wx.lib.scrolledpanel as scrolled
import os
import json
import subprocess
import argparse
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_config_path():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=None)
    args, _ = parser.parse_known_args()
    if args.config:
        return os.path.abspath(args.config)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


DEFAULT_CONFIG = {
    "llama_dir": "",
    "models_dir": "",
    "port": 8080,
}


def load_config(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(path, cfg):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Per-model parameter definitions
# ---------------------------------------------------------------------------

# (key, display label, cli flag)
MODEL_PARAMS = [
    ("ctx_size",         "Context Size",     "--ctx-size"),
    ("temperature",      "Temperature",      "--temp"),
    ("top_k",            "Top K",            "--top-k"),
    ("top_p",            "Top P",            "--top-p"),
    ("min_p",            "Min P",            "--min-p"),
    ("presence_penalty", "Presence Penalty", "--presence-penalty"),
    ("repeat_penalty",   "Repeat Penalty",   "--repeat-penalty"),
    ("image_min_tokens", "Image Min Tokens", "--image-min-tokens"),
    ("image_max_tokens", "Image Max Tokens", "--image-max-tokens"),
]


# ---------------------------------------------------------------------------
# Model-folder validation
# ---------------------------------------------------------------------------

class ModelInfo:
    """Result of inspecting a model folder."""
    def __init__(self, folder_path):
        self.folder_path = folder_path
        self.model_file = None      # path to the primary .gguf
        self.mmproj_file = None     # path to the mmproj .gguf (optional)
        self.valid = False
        self.error = ""
        self._inspect()

    def _inspect(self):
        try:
            entries = os.listdir(self.folder_path)
        except PermissionError:
            self.error = "Cannot read folder (permission denied)"
            return

        gguf_files = [e for e in entries
                      if e.lower().endswith(".gguf")
                      and os.path.isfile(os.path.join(self.folder_path, e))]

        if len(gguf_files) == 0:
            self.error = "No .gguf files found"
        elif len(gguf_files) == 1:
            self.model_file = os.path.join(self.folder_path, gguf_files[0])
            self.valid = True
        elif len(gguf_files) == 2:
            mmproj = [f for f in gguf_files if "mmproj" in f.lower()]
            model  = [f for f in gguf_files if "mmproj" not in f.lower()]
            if len(mmproj) == 1 and len(model) == 1:
                self.model_file  = os.path.join(self.folder_path, model[0])
                self.mmproj_file = os.path.join(self.folder_path, mmproj[0])
                self.valid = True
            else:
                self.error = (
                    "2 .gguf files found but could not determine which is the "
                    "mmproj file (one must contain 'mmproj' in its name)"
                )
        else:
            self.error = f"{len(gguf_files)} .gguf files found (expected 1 or 2)"


# ---------------------------------------------------------------------------
# Model list panel
# ---------------------------------------------------------------------------

COLOR_VALID   = wx.Colour(220, 255, 220)   # light green
COLOR_INVALID = wx.Colour(255, 220, 220)   # light red
COLOR_SELECTED_VALID   = wx.Colour(100, 200, 100)
COLOR_SELECTED_INVALID = wx.Colour(200, 100, 100)


class ModelItem(wx.Panel):
    def __init__(self, parent, folder_name, model_info, on_select_cb, on_double_click_cb=None):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.folder_name = folder_name
        self.model_info  = model_info
        self.on_select_cb = on_select_cb
        self.on_double_click_cb = on_double_click_cb
        self.selected = False

        self._base_color = COLOR_VALID if model_info.valid else COLOR_INVALID
        self.SetBackgroundColour(self._base_color)

        sizer = wx.BoxSizer(wx.VERTICAL)
        bold_font = self.GetFont()
        bold_font.SetWeight(wx.FONTWEIGHT_BOLD)

        name_lbl = wx.StaticText(self, label=folder_name)
        name_lbl.SetFont(bold_font)

        if model_info.valid:
            detail = os.path.basename(model_info.model_file)
            if model_info.mmproj_file:
                detail += f"  +  mmproj: {os.path.basename(model_info.mmproj_file)}"
        else:
            detail = f"Invalid: {model_info.error}"

        detail_lbl = wx.StaticText(self, label=detail)
        sizer.Add(name_lbl,  0, wx.ALL, 4)
        sizer.Add(detail_lbl, 0, wx.LEFT | wx.BOTTOM, 8)
        self.SetSizer(sizer)

        # Bind clicks on panel and children
        for widget in (self, name_lbl, detail_lbl):
            widget.Bind(wx.EVT_LEFT_DOWN, self._on_click)
            widget.Bind(wx.EVT_LEFT_DCLICK, self._on_double_click)

    def _on_click(self, _evt):
        self.on_select_cb(self)

    def _on_double_click(self, _evt):
        self.on_select_cb(self)
        if self.on_double_click_cb:
            self.on_double_click_cb(self)

    def set_selected(self, selected):
        self.selected = selected
        if selected:
            col = COLOR_SELECTED_VALID if self.model_info.valid else COLOR_SELECTED_INVALID
        else:
            col = self._base_color
        self.SetBackgroundColour(col)
        self.Refresh()


# ---------------------------------------------------------------------------
# Model settings dialog
# ---------------------------------------------------------------------------

class ModelSettingsDialog(wx.Dialog):
    """Per-model parameter settings (checkbox + value for each param)."""

    def __init__(self, parent, folder_name, model_settings):
        super().__init__(parent, title=f"Model Settings — {folder_name}",
                         style=wx.DEFAULT_DIALOG_STYLE)
        # model_settings: dict keyed by param key -> {"enabled": bool, "value": str}
        self._settings = {k: dict(v) for k, v in model_settings.items()}
        self._rows = {}   # key -> (CheckBox, TextCtrl)
        self._build_ui()
        self.Fit()

    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        grid.AddGrowableCol(1, 1)

        for key, label, _flag in MODEL_PARAMS:
            s = self._settings.get(key, {"enabled": False, "value": ""})
            chk = wx.CheckBox(self, label=label)
            chk.SetValue(s.get("enabled", False))
            txt = wx.TextCtrl(self, value=s.get("value", ""),
                              size=wx.Size(60, -1))
            txt.SetMaxLength(8)
            grid.Add(chk, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(txt, 0, wx.ALIGN_CENTER_VERTICAL)
            self._rows[key] = (chk, txt)

        main.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        main.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                 wx.EXPAND | wx.ALL, 8)
        self.SetSizer(main)

    def get_settings(self):
        result = {}
        for key, _label, _flag in MODEL_PARAMS:
            chk, txt = self._rows[key]
            result[key] = {
                "enabled": chk.GetValue(),
                "value":   txt.GetValue().strip(),
            }
        return result


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(wx.Dialog):
    def __init__(self, parent, cfg):
        super().__init__(parent, title="Settings", style=wx.DEFAULT_DIALOG_STYLE)
        self.cfg = dict(cfg)
        self._build_ui()
        self.SetMinSize((540, 240))
        self.Fit()

    def _make_dir_row(self, sizer, label, attr):
        row = wx.BoxSizer(wx.HORIZONTAL)
        lbl  = wx.StaticText(self, label=label, size=wx.Size(160, -1))
        ctrl = wx.TextCtrl(self, value=str(self.cfg.get(attr, "")), size=wx.Size(260, -1))
        btn  = wx.Button(self, label="Browse…", size=wx.Size(80, -1))
        row.Add(lbl,  0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        row.Add(btn,  0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        def on_browse(_evt, c=ctrl):
            with wx.DirDialog(self, f"Select {label}", defaultPath=c.GetValue()) as d:
                if d.ShowModal() == wx.ID_OK:
                    c.SetValue(d.GetPath())
        btn.Bind(wx.EVT_BUTTON, on_browse)
        return ctrl

    def _make_spin_row(self, sizer, label, attr, lo, hi):
        row  = wx.BoxSizer(wx.HORIZONTAL)
        lbl  = wx.StaticText(self, label=label, size=wx.Size(160, -1))
        spin = wx.SpinCtrl(self, value=str(self.cfg.get(attr, 0)),
                           min=lo, max=hi, size=wx.Size(100, -1))
        row.Add(lbl,  0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row.Add(spin, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(row, 0, wx.ALL, 6)
        return spin

    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)
        grid = wx.BoxSizer(wx.VERTICAL)

        self._llama_dir_ctrl  = self._make_dir_row(grid, "llama.cpp directory:", "llama_dir")
        self._models_dir_ctrl = self._make_dir_row(grid, "Models directory:",    "models_dir")
        self._port_spin       = self._make_spin_row(grid, "Port:",               "port", 1, 65535)

        main.Add(grid, 1, wx.EXPAND | wx.ALL, 8)
        main.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                 wx.EXPAND | wx.ALL, 8)
        self.SetSizer(main)

    def get_config(self):
        self.cfg["llama_dir"]  = self._llama_dir_ctrl.GetValue().strip()
        self.cfg["models_dir"] = self._models_dir_ctrl.GetValue().strip()
        self.cfg["port"]       = self._port_spin.GetValue()
        return self.cfg


# ---------------------------------------------------------------------------
# Main frame
# ---------------------------------------------------------------------------

class LlamaLauncherFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Llama Launcher", size=(700, 580))
        self.config_path   = get_config_path()
        self.cfg           = load_config(self.config_path)
        self.selected_item = None
        self._build_ui()
        self._refresh_model_list()
        self.Centre()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        panel = wx.Panel(self)
        vbox  = wx.BoxSizer(wx.VERTICAL)

        # --- Top bar: models dir + settings button --------------------
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticText(panel, label="Models dir:"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._models_dir_ctrl = wx.TextCtrl(panel, value=self.cfg.get("models_dir", ""))
        top.Add(self._models_dir_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        browse_btn = wx.Button(panel, label="Browse…")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_models)
        top.Add(browse_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        refresh_btn = wx.Button(panel, label="Refresh")
        refresh_btn.Bind(wx.EVT_BUTTON, lambda _e: self._refresh_model_list())
        top.Add(refresh_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        settings_btn = wx.Button(panel, label="Settings…")
        settings_btn.Bind(wx.EVT_BUTTON, self._on_settings)
        top.Add(settings_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        vbox.Add(top, 0, wx.EXPAND | wx.ALL, 8)

        # --- Model list (scrolled) ------------------------------------
        vbox.Add(wx.StaticText(panel, label="Select a model folder:"),
                 0, wx.LEFT | wx.BOTTOM, 8)

        self._scroll = scrolled.ScrolledPanel(panel, style=wx.BORDER_SUNKEN)
        self._scroll.SetupScrolling(scroll_x=False, scroll_y=True)
        self._list_sizer = wx.BoxSizer(wx.VERTICAL)
        self._scroll.SetSizer(self._list_sizer)
        vbox.Add(self._scroll, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- Status / command preview ---------------------------------
        vbox.Add(wx.StaticText(panel, label="Command preview:"),
                 0, wx.LEFT | wx.BOTTOM, 4)
        self._cmd_preview = wx.TextCtrl(panel, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                        size=(-1, 60))
        vbox.Add(self._cmd_preview, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- Bottom button row ----------------------------------------
        btn_row = wx.BoxSizer(wx.HORIZONTAL)

        self._model_settings_btn = wx.Button(panel, label="Model Settings…")
        self._model_settings_btn.Disable()
        self._model_settings_btn.Bind(wx.EVT_BUTTON, self._on_model_settings)
        btn_row.Add(self._model_settings_btn, 0, wx.RIGHT, 12)

        self._launch_btn = wx.Button(panel, label="Launch llama-server")
        self._launch_btn.SetFont(self._launch_btn.GetFont().Bold())
        self._launch_btn.Disable()
        self._launch_btn.Bind(wx.EVT_BUTTON, self._on_launch)
        btn_row.Add(self._launch_btn, 0)

        vbox.Add(btn_row, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        panel.SetSizer(vbox)

    # ------------------------------------------------------------------
    # Model list
    # ------------------------------------------------------------------

    def _refresh_model_list(self):
        # Sync models_dir from text ctrl
        self.cfg["models_dir"] = self._models_dir_ctrl.GetValue().strip()
        save_config(self.config_path, self.cfg)

        # Clear list
        for child in self._scroll.GetChildren():
            child.Destroy()
        self._list_sizer.Clear()
        self.selected_item = None
        self._launch_btn.Disable()
        self._model_settings_btn.Disable()
        self._cmd_preview.SetValue("")

        models_dir = self.cfg.get("models_dir", "")
        if not models_dir or not os.path.isdir(models_dir):
            msg = wx.StaticText(self._scroll,
                                label="Set a valid models directory above.")
            self._list_sizer.Add(msg, 0, wx.ALL, 12)
            self._scroll.Layout()
            self._scroll.SetupScrolling(scroll_x=False, scroll_y=True)
            return

        try:
            folders = sorted(
                d for d in os.listdir(models_dir)
                if os.path.isdir(os.path.join(models_dir, d))
            )
        except PermissionError:
            wx.MessageBox("Cannot read models directory.", "Error",
                          wx.OK | wx.ICON_ERROR, self)
            return

        if not folders:
            msg = wx.StaticText(self._scroll, label="No sub-folders found in models directory.")
            self._list_sizer.Add(msg, 0, wx.ALL, 12)
        else:
            for fname in folders:
                fpath = os.path.join(models_dir, fname)
                info  = ModelInfo(fpath)
                item  = ModelItem(self._scroll, fname, info, self._on_item_selected, self._on_launch)
                self._list_sizer.Add(item, 0, wx.EXPAND | wx.ALL, 3)

        self._scroll.Layout()
        self._scroll.SetupScrolling(scroll_x=False, scroll_y=True)

    def _on_item_selected(self, item):
        if self.selected_item and self.selected_item is not item:
            self.selected_item.set_selected(False)
        self.selected_item = item
        item.set_selected(True)

        if item.model_info.valid:
            self._launch_btn.Enable()
            self._model_settings_btn.Enable()
            self._cmd_preview.SetValue(self._build_command(item.folder_name, item.model_info))
        else:
            self._launch_btn.Disable()
            self._model_settings_btn.Enable()  # can still configure settings for invalid models
            self._cmd_preview.SetValue(f"Invalid model folder: {item.model_info.error}")

    # ------------------------------------------------------------------
    # Per-model settings helpers
    # ------------------------------------------------------------------

    def _get_model_settings(self, folder_name):
        """Return the stored settings dict for a model folder (may be empty)."""
        return self.cfg.setdefault("model_settings", {}).get(folder_name, {})

    def _set_model_settings(self, folder_name, settings):
        self.cfg.setdefault("model_settings", {})[folder_name] = settings
        save_config(self.config_path, self.cfg)

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_command(self, folder_name, model_info):
        llama_dir = self.cfg.get("llama_dir", "")
        exe = os.path.join(llama_dir, "llama-server.exe") if llama_dir else "llama-server.exe"

        parts = [
            exe,
            "-m", model_info.model_file,
            "--port", str(self.cfg.get("port", 8080)),
        ]

        model_settings = self._get_model_settings(folder_name)
        for key, _label, flag in MODEL_PARAMS:
            s = model_settings.get(key, {})
            if s.get("enabled") and s.get("value", "").strip():
                parts += [flag, s["value"].strip()]

        if model_info.mmproj_file:
            parts += ["--mmproj", model_info.mmproj_file]

        return " ".join(f'"{p}"' if " " in str(p) else str(p) for p in parts)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_browse_models(self, _evt):
        current = self._models_dir_ctrl.GetValue()
        with wx.DirDialog(self, "Select models directory",
                          defaultPath=current if os.path.isdir(current) else "") as d:
            if d.ShowModal() == wx.ID_OK:
                self._models_dir_ctrl.SetValue(d.GetPath())
                self._refresh_model_list()

    def _on_settings(self, _evt):
        # Sync current models_dir into cfg before opening dialog
        self.cfg["models_dir"] = self._models_dir_ctrl.GetValue().strip()
        dlg = SettingsDialog(self, self.cfg)
        if dlg.ShowModal() == wx.ID_OK:
            self.cfg = dlg.get_config()
            save_config(self.config_path, self.cfg)
            self._models_dir_ctrl.SetValue(self.cfg.get("models_dir", ""))
            self._refresh_model_list()
        dlg.Destroy()

    def _on_model_settings(self, _evt):
        if not self.selected_item:
            return
        folder_name = self.selected_item.folder_name
        current = self._get_model_settings(folder_name)
        dlg = ModelSettingsDialog(self, folder_name, current)
        if dlg.ShowModal() == wx.ID_OK:
            self._set_model_settings(folder_name, dlg.get_settings())
            # Refresh command preview if the selected model is valid
            if self.selected_item.model_info.valid:
                self._cmd_preview.SetValue(
                    self._build_command(folder_name, self.selected_item.model_info)
                )
        dlg.Destroy()

    def _port_in_use(self, port):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True   # got a response — something is listening
        except Exception:
            return False

    def _on_launch(self, _evt):
        if not self.selected_item or not self.selected_item.model_info.valid:
            return

        port = self.cfg.get("port", 8080)

        if self._port_in_use(port):
            wx.MessageBox(
                f"A server is already running on port {port}.",
                "Port in use", wx.OK | wx.ICON_WARNING, self
            )
            return

        llama_dir = self.cfg.get("llama_dir", "")
        exe = os.path.join(llama_dir, "llama-server.exe") if llama_dir else "llama-server.exe"
        if not os.path.isfile(exe):
            wx.MessageBox(
                f"llama-server.exe not found at:\n{exe}\n\n"
                "Please set the llama.cpp directory in Settings.",
                "Executable not found", wx.OK | wx.ICON_ERROR, self
            )
            return

        try:
            folder_name = self.selected_item.folder_name
            model_info  = self.selected_item.model_info
            args = [exe,
                    "-m", model_info.model_file,
                    "--port", str(port)]

            model_settings = self._get_model_settings(folder_name)
            for key, _label, flag in MODEL_PARAMS:
                s = model_settings.get(key, {})
                if s.get("enabled") and s.get("value", "").strip():
                    args += [flag, s["value"].strip()]

            if model_info.mmproj_file:
                args += ["--mmproj", model_info.mmproj_file]

            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as exc:
            wx.MessageBox(f"Failed to launch llama-server:\n{exc}",
                          "Error", wx.OK | wx.ICON_ERROR, self)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = wx.App(False)
    frame = LlamaLauncherFrame()
    frame.Show()
    app.MainLoop()
