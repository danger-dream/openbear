// Monaco does not resolve CSS variables. Only its neutral chrome is bridged;
// inherited vs/vs-dark token rules keep syntax highlighting and diagnostics.
export function editorTheme(dark, readChannel) {
  const color = (role, opacity) => {
    const rgb = String(readChannel(`--ob-${role}-rgb`)).trim().split(/\s+/).map(Number);
    if (rgb.length !== 3 || rgb.some(v => !Number.isFinite(v) || v < 0 || v > 255)) throw new Error(`Missing editor theme color: ${role}`);
    const hex = rgb.map(v => Math.round(v).toString(16).padStart(2, '0')).join('');
    return `#${hex}${opacity === undefined ? '' : Math.round(opacity * 255).toString(16).padStart(2, '0')}`;
  };
  return {
    base: dark ? 'vs-dark' : 'vs', inherit: true, rules: [],
    colors: {
      'editor.background': color('surface'),
      'editor.foreground': color('text'),
      'editorGutter.background': color('surface'),
      'editorLineNumber.foreground': color('text-muted'),
      'editorLineNumber.activeForeground': color('text-subtle'),
      'editorCursor.foreground': color('text-strong'),
      'editor.selectionBackground': color('blue', dark ? .30 : .18),
      'editor.inactiveSelectionBackground': color('blue', .10),
      'editor.lineHighlightBackground': color('surface-soft'),
      'editorWidget.background': color('surface-raised'),
      'editorWidget.foreground': color('text'),
      'editorWidget.border': color('border', dark ? .09 : .08),
      'editorSuggestWidget.background': color('surface-raised'),
      'editorSuggestWidget.foreground': color('text'),
      'editorSuggestWidget.selectedBackground': color('surface-soft'),
      'editorSuggestWidget.highlightForeground': color('blue'),
      'editorHoverWidget.background': color('surface-raised'),
      'editorHoverWidget.foreground': color('text'),
      'editorHoverWidget.border': color('border', dark ? .09 : .08),
      'input.background': color('surface'),
      'input.foreground': color('text'),
      'focusBorder': color('blue'),
      'scrollbarSlider.background': color('border', .18),
      'scrollbarSlider.hoverBackground': color('border', .28),
      'scrollbarSlider.activeBackground': color('border', .35),
    },
  };
}
