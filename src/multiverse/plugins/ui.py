"""The 1.x contract for a plugin's own dashboard screens that edit settings.

A plugin screen imports from here, never from `multiverse.ui`, and only plugin
screens do: the command line must not import Textual, which is why a plugin
names its screens as "module:Class" and the dashboard imports them. This module
is part of the API version.

    from multiverse.plugins.ui import FieldScreen, field

    class MyScreen(FieldScreen):
        # Built by the dashboard as MyScreen(root=Path, backend=app.backend).
        def compose(self):
            yield ...                      # one Static with the class "cerror" is the error line
        def action_save(self):             # Ctrl+Enter
            ...                            # self.dismiss("a status line") when done

Promised from API 1.7 (it changes only by adding, or in a new major version):

- `FieldScreen(root, backend)`: a ModalScreen with `.root` (a Path), `.backend`,
  `.fields`, `.rows`, `.widgets` and `.saving`. Esc calls `action_close` (it
  dismisses with None) and Ctrl+Enter calls `action_save`, which the plugin's
  screen implements. `dismiss("text")` makes that text the dashboard's status
  line; any other result sets nothing.
- Helpers: `field_rows(items)` returns one row per `Field` to mount, in order;
  `values()` gives each widget's value by field key; `error(text)` shows text in
  the error line; `refresh_rows()` shows or hides rows by `applies`.
- Hooks a screen may override: `applies(item, values)` says whether a field is
  shown given every field's current value, and `changed()` runs after any edit.
- `Field`: where a setting lives (`path`, a tuple of keys), how it is labelled
  and edited (`kind`: text, int, float, nullable or choice; `options` for a
  choice, as (value, label) pairs where None means "not set"), its `placeholder`,
  `initial` value, `group`, `role` and `help`. (Not api.Field, which declares
  settings for `mv.dashboard.settings(fields=...)` and needs no screen.)
- `field(key, path, label, kind, data, placeholder="", options=(), **extra)`
  makes a Field whose starting value is read from `data` at `path`.
- `convert(item, value)` turns a widget's value into what the file holds, or
  MISSING when it is blank (a blank field means the setting's default); it
  raises ValueError, naming the field, for text that is not a number.
- `MISSING` and `BOOL_OPTIONS` (the true/false options of a choice).
- The look of a field screen (rows, labels, the error line, section titles,
  notes and a field's help) comes with `FieldScreen`: a plugin's screen needs
  nothing from the dashboard's stylesheet for its fields. Style its own widgets
  with `DEFAULT_CSS` scoped to its class name.

Not promised: the rest of `multiverse.ui`, including other helpers in
`multiverse.ui.settings`, widget ids, the CSS class names beyond those above,
and the rows the dashboard's own Plugins screen builds.
"""
from ..ui.settings import BOOL_OPTIONS, MISSING, Field, FieldScreen, convert, field

__all__ = ["BOOL_OPTIONS", "MISSING", "Field", "FieldScreen", "convert", "field"]
