export function chooseActiveTurnIndex(rows = [], viewportTop = 0, viewportHeight = 0, options = {}) {
	if (!Array.isArray(rows) || !rows.length) return 0;
	const preferred = options?.preferredIndex;
	if (preferred !== null && preferred !== undefined && Number.isFinite(Number(preferred))) {
		const preferredIndex = Math.max(0, Number(preferred));
		if (rows.some((row) => Math.max(0, Number(row?.index || 0)) === preferredIndex)) return preferredIndex;
	}
	if (options?.atBottom) return Math.max(0, Number(rows.at(-1)?.index || 0));
	const safeHeight = Math.max(0, Number(viewportHeight || 0));
	const anchorY = Number(viewportTop || 0) + Math.min(220, safeHeight * 0.28);

	// A long turn may start far above the viewport while the user is still reading
	// its middle. Containment must therefore win over distance to turn starts.
	for (const row of rows) {
		const top = Number(row?.top || 0);
		const bottom = Number(row?.bottom ?? top);
		if (top <= anchorY && bottom >= anchorY) return Math.max(0, Number(row?.index || 0));
	}

	let bestIndex = Math.max(0, Number(rows[0]?.index || 0));
	let bestDistance = Number.POSITIVE_INFINITY;
	for (const row of rows) {
		const top = Number(row?.top || 0);
		const bottom = Number(row?.bottom ?? top);
		const distance = anchorY < top ? top - anchorY : Math.max(0, anchorY - bottom);
		if (distance < bestDistance) {
			bestDistance = distance;
			bestIndex = Math.max(0, Number(row?.index || 0));
		}
	}
	return bestIndex;
}
