import wx
import wx.lib.scrolledpanel as scrolled
import os
import sys
import json
import subprocess
import argparse
import urllib.request
import urllib.error

_SERVER_BIN = "llama-server.exe" if sys.platform == "win32" else "llama-server"

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

# (key, display label, cli flag[, choices])
# Entries with a choices list render as a drop-down; others render as a text field.
MODEL_PARAMS = [
    ("ctx_size",          "Context Size",      "--ctx-size"),
    ("reasoning",         "Reasoning",         "--reasoning",       ["Auto", "On", "Off"]),
    ("reasoning_budget",  "Reasoning Budget",  "--reasoning-budget"),
    ("temperature",       "Temperature",       "--temp"),
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

_CT_TYPES = ["string", "boolean", "integer", "float"]


def _coerce_ct_value(pair):
    """Convert a pair's string value to the correct Python type for JSON serialization."""
    t = pair.get("type", "string")
    v = pair.get("value", "")
    if t == "boolean":
        return v.lower() != "false"
    if t == "integer":
        try:
            return int(v)
        except (ValueError, TypeError):
            return v
    if t == "float":
        try:
            return float(v)
        except (ValueError, TypeError):
            return v
    return v  # string


class ChatTemplateKwargsDialog(wx.Dialog):
    """Editor for an arbitrary list of key/value pairs passed as a JSON object
    to --chat-template-kwargs."""

    def __init__(self, parent, pairs):
        super().__init__(parent, title="Chat Template Kwargs",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        # pairs: list of {"key": str, "type": str, "value": str}
        self._pair_rows = []  # (key_ctrl, type_ctrl, val_ctrl, bool_ctrl, remove_btn, row_sizer)
        self._build_ui(pairs)
        self.Fit()
        self.SetMinSize(self.GetSize())

    def _build_ui(self, initial_pairs):
        main = wx.BoxSizer(wx.VERTICAL)

        hdr = wx.BoxSizer(wx.HORIZONTAL)
        hdr.Add(wx.StaticText(self, label="Key"),   0, wx.RIGHT, 74)
        hdr.Add(wx.StaticText(self, label="Type"),  0, wx.RIGHT, 60)
        hdr.Add(wx.StaticText(self, label="Value"), 0)
        main.Add(hdr, 0, wx.LEFT | wx.TOP, 12)

        self._pairs_sizer = wx.BoxSizer(wx.VERTICAL)
        main.Add(self._pairs_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        add_btn = wx.Button(self, label="Add Pair")
        add_btn.Bind(wx.EVT_BUTTON, lambda _evt: self._add_row())
        main.Add(add_btn, 0, wx.LEFT | wx.TOP, 12)

        main.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                 wx.EXPAND | wx.ALL, 8)
        self.SetSizer(main)

        for pair in initial_pairs:
            self._add_row(pair.get("key", ""),
                          pair.get("type", "string"),
                          pair.get("value", ""))

    def _add_row(self, key="", type_="string", value=""):
        row_sizer  = wx.BoxSizer(wx.HORIZONTAL)
        key_ctrl   = wx.TextCtrl(self, value=key,   size=wx.Size(120, -1))
        type_ctrl  = wx.Choice(self, choices=_CT_TYPES, size=wx.Size(80, -1))
        val_ctrl   = wx.TextCtrl(self, value=value, size=wx.Size(160, -1))
        bool_ctrl  = wx.Choice(self, choices=["true", "false"], size=wx.Size(160, -1))
        remove_btn = wx.Button(self, label="X",     size=wx.Size(28, -1))

        # Initialise type selection and value controls
        type_idx = _CT_TYPES.index(type_) if type_ in _CT_TYPES else 0
        type_ctrl.SetSelection(type_idx)
        is_bool = (type_ == "boolean")
        if is_bool:
            bool_ctrl.SetSelection(0 if value.lower() != "false" else 1)
        val_ctrl.Show(not is_bool)
        bool_ctrl.Show(is_bool)

        row_sizer.Add(key_ctrl,   0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        row_sizer.Add(type_ctrl,  0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        row_sizer.Add(val_ctrl,   0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        row_sizer.Add(bool_ctrl,  0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        row_sizer.Add(remove_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        self._pairs_sizer.Add(row_sizer, 0, wx.BOTTOM, 3)
        row_data = (key_ctrl, type_ctrl, val_ctrl, bool_ctrl, remove_btn, row_sizer)
        self._pair_rows.append(row_data)

        def on_type_change(_evt, vc=val_ctrl, bc=bool_ctrl, rs=row_sizer):
            now_bool = type_ctrl.GetStringSelection() == "boolean"
            vc.Show(not now_bool)
            bc.Show(now_bool)
            rs.Layout()
            self._pairs_sizer.Layout()
            self.Layout()

        type_ctrl.Bind(wx.EVT_CHOICE, on_type_change)
        remove_btn.Bind(wx.EVT_BUTTON,
                        lambda _evt, rd=row_data: self._remove_row(rd))
        self.Layout()
        self.Fit()

    def _remove_row(self, row_data):
        key_ctrl, type_ctrl, val_ctrl, bool_ctrl, remove_btn, row_sizer = row_data
        self._pair_rows.remove(row_data)
        for widget in (key_ctrl, type_ctrl, val_ctrl, bool_ctrl, remove_btn):
            widget.Destroy()
        self._pairs_sizer.Remove(row_sizer)
        self.Layout()
        self.Fit()

    def get_pairs(self):
        pairs = []
        for key_ctrl, type_ctrl, val_ctrl, bool_ctrl, _btn, _sizer in self._pair_rows:
            k = key_ctrl.GetValue().strip()
            t = type_ctrl.GetStringSelection()
            v = (bool_ctrl.GetStringSelection()
                 if t == "boolean" else val_ctrl.GetValue().strip())
            if k:
                pairs.append({"key": k, "type": t, "value": v})
        return pairs


class ModelSettingsDialog(wx.Dialog):
    """Per-model parameter settings (checkbox + value for each param)."""

    def __init__(self, parent, folder_name, model_settings):
        super().__init__(parent, title=f"Model Settings — {folder_name}",
                         style=wx.DEFAULT_DIALOG_STYLE)
        # model_settings: dict keyed by param key -> {"enabled": bool, "value": str}
        self._settings = {k: dict(v) for k, v in model_settings.items()}
        self._rows = {}   # key -> (CheckBox, TextCtrl)
        self._ct_pairs = list(
            self._settings.get("chat_template_kwargs", {}).get("pairs", [])
        )
        self._build_ui()
        self.Fit()

    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        grid.AddGrowableCol(1, 1)

        for key, label, _flag, *rest in MODEL_PARAMS:
            choices = rest[0] if rest else None
            s = self._settings.get(key, {"enabled": False, "value": ""})
            enabled = s.get("enabled", False)
            chk = wx.CheckBox(self, label=label)
            chk.SetValue(enabled)
            if choices:
                ctrl = wx.Choice(self, choices=choices)
                saved = s.get("value", "").lower()
                idx = next((i for i, c in enumerate(choices) if c.lower() == saved), 0)
                ctrl.SetSelection(idx)
            else:
                ctrl = wx.TextCtrl(self, value=s.get("value", ""),
                                   size=wx.Size(60, -1))
                ctrl.SetMaxLength(8)
            ctrl.Enable(enabled)
            grid.Add(chk, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
            self._rows[key] = (chk, ctrl, choices)
            chk.Bind(wx.EVT_CHECKBOX, lambda evt, c=ctrl: c.Enable(evt.IsChecked()))

        main.Add(grid, 1, wx.EXPAND | wx.ALL, 12)

        # --- Chat Template Kwargs row ------------------------------------
        ct_row = wx.BoxSizer(wx.HORIZONTAL)
        self._ct_chk = wx.CheckBox(self, label="Chat Template Kwargs")
        self._ct_chk.SetValue(
            self._settings.get("chat_template_kwargs", {}).get("enabled", False)
        )
        ct_row.Add(self._ct_chk, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        n = len(self._ct_pairs)
        self._ct_summary = wx.StaticText(
            self, label=f"{n} pair(s)" if n else "(none)"
        )
        ct_row.Add(self._ct_summary, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        edit_ct_btn = wx.Button(self, label="Edit...")
        edit_ct_btn.Bind(wx.EVT_BUTTON, self._on_edit_ct_kwargs)
        ct_row.Add(edit_ct_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        main.Add(ct_row, 0, wx.LEFT | wx.BOTTOM, 12)

        main.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                 wx.EXPAND | wx.ALL, 8)
        self.SetSizer(main)

    def _on_edit_ct_kwargs(self, _evt):
        dlg = ChatTemplateKwargsDialog(self, self._ct_pairs)
        if dlg.ShowModal() == wx.ID_OK:
            self._ct_pairs = dlg.get_pairs()
            n = len(self._ct_pairs)
            self._ct_summary.SetLabel(f"{n} pair(s)" if n else "(none)")
        dlg.Destroy()

    def get_settings(self):
        result = {}
        for key, _label, _flag, *rest in MODEL_PARAMS:
            choices = rest[0] if rest else None
            chk, ctrl, _ = self._rows[key]
            if choices:
                idx = ctrl.GetSelection()
                val = choices[idx].lower() if idx != wx.NOT_FOUND else ""
            else:
                val = ctrl.GetValue().strip()
            result[key] = {
                "enabled": chk.GetValue(),
                "value":   val,
            }
        result["chat_template_kwargs"] = {
            "enabled": self._ct_chk.GetValue(),
            "pairs":   self._ct_pairs,
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
        exe = os.path.join(llama_dir, _SERVER_BIN) if llama_dir else _SERVER_BIN

        parts = [
            exe,
            "-m", model_info.model_file,
            "--port", str(self.cfg.get("port", 8080)),
        ]

        model_settings = self._get_model_settings(folder_name)
        for key, _label, flag, *_ in MODEL_PARAMS:
            s = model_settings.get(key, {})
            if s.get("enabled") and s.get("value", "").strip():
                parts += [flag, s["value"].strip()]

        ct = model_settings.get("chat_template_kwargs", {})
        if ct.get("enabled") and ct.get("pairs"):
            obj = {p["key"]: _coerce_ct_value(p) for p in ct["pairs"] if p.get("key")}
            if obj:
                parts += ["--chat-template-kwargs", json.dumps(obj, separators=(",", ":"))]

        if model_info.mmproj_file:
            parts += ["--mmproj", model_info.mmproj_file]

        def _quote(s):
            s = str(s)
            if any(c in s for c in (' ', '"', '{', '}')):
                return '"' + s.replace('"', '\\"') + '"'
            return s

        return " ".join(_quote(p) for p in parts)

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
        server_binary = os.path.join(llama_dir, _SERVER_BIN) if llama_dir else _SERVER_BIN
        if not os.path.isfile(server_binary):
            wx.MessageBox(
                f"{_SERVER_BIN} not found at:\n{server_binary}\n\n"
                "Please set the llama.cpp directory in Settings.",
                "Binary not found", wx.OK | wx.ICON_ERROR, self
            )
            return

        try:
            folder_name = self.selected_item.folder_name
            model_info  = self.selected_item.model_info
            args = [server_binary,
                    "-m", model_info.model_file,
                    "--port", str(port)]

            model_settings = self._get_model_settings(folder_name)
            for key, _label, flag, *_ in MODEL_PARAMS:
                s = model_settings.get(key, {})
                if s.get("enabled") and s.get("value", "").strip():
                    args += [flag, s["value"].strip()]

            ct = model_settings.get("chat_template_kwargs", {})
            if ct.get("enabled") and ct.get("pairs"):
                obj = {p["key"]: _coerce_ct_value(p) for p in ct["pairs"] if p.get("key")}
                if obj:
                    args += ["--chat-template-kwargs", json.dumps(obj, separators=(",", ":"))]

            if model_info.mmproj_file:
                args += ["--mmproj", model_info.mmproj_file]

            if sys.platform == "win32":
                subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                subprocess.Popen(args)
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
