"""Save filter choices in the browser before a server round trip can fail."""
import json

from nicegui import ui


def bind_filter_url(control: ui.select, parameter: str, defaults: dict) -> None:
    # NiceGUI's select event carries an option index, not its database value.
    # Keep the mapping on the rendered element, including after options change.
    update_filter_keys(control)
    control.on('update:model-value', js_handler=f'''event => {{
        const element = document.getElementById({json.dumps(control.html_id)});
        const keys = JSON.parse(element.getAttribute('data-filter-keys'));
        const value = keys[event?.value];
        if (value === undefined) return;
        const url = new URL(window.location.href);
        for (const [key, fallback] of Object.entries({json.dumps(defaults)})) {{
            if (!url.searchParams.has(key)) url.searchParams.set(key, fallback);
        }}
        url.searchParams.set({json.dumps(parameter)}, value);
        history.replaceState(history.state, '', url);
    }}''')


def update_filter_keys(control: ui.select) -> None:
    control.props['data-filter-keys'] = json.dumps(list(control.options))
