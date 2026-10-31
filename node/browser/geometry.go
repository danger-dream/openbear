package browser

import (
	"context"
	"math"
)

// point is a CSS-pixel coordinate.
type point struct{ X, Y float64 }

func (p point) obj() *Obj { return O("x", p.X, "y", p.Y) }

// transform maps between quadrilaterals via a homography (geometry.transform).
func transform(ctx context.Context, p point, source, dest []float64) (point, error) {
	var rows [8][9]float64
	for i := 0; i < 8; i += 2 {
		x, y, u, v := source[i], source[i+1], dest[i], dest[i+1]
		rows[i] = [9]float64{x, y, 1, 0, 0, 0, -u * x, -u * y, u}
		rows[i+1] = [9]float64{0, 0, 0, x, y, 1, -v * x, -v * y, v}
	}
	for col := 0; col < 8; col++ {
		pivot := col
		for row := col; row < 8; row++ {
			if math.Abs(rows[row][col]) > math.Abs(rows[pivot][col]) {
				pivot = row
			}
		}
		if math.Abs(rows[pivot][col]) < 1e-10 {
			return point{}, browserError(ctx, "element_not_visible", "", nil)
		}
		rows[col], rows[pivot] = rows[pivot], rows[col]
		scale := rows[col][col]
		for k := range rows[col] {
			rows[col][k] /= scale
		}
		for row := 0; row < 8; row++ {
			if row != col {
				scale := rows[row][col]
				for k := range rows[row] {
					rows[row][k] -= scale * rows[col][k]
				}
			}
		}
	}
	a, b, c, d, e, f, g, h := rows[0][8], rows[1][8], rows[2][8], rows[3][8], rows[4][8], rows[5][8], rows[6][8], rows[7][8]
	den := g*p.X + h*p.Y + 1
	if math.Abs(den) < 1e-10 {
		return point{}, browserError(ctx, "element_not_visible", "", nil)
	}
	return point{(a*p.X + b*p.Y + c) / den, (d*p.X + e*p.Y + f) / den}, nil
}

func viewportQuad(w, h float64) []float64 { return []float64{0, 0, w, 0, w, h, 0, h} }

// clickablePoint clips actual content quads to the viewport and returns the
// centroid of the first visible polygon with area > 1.
func clickablePoint(quads [][]float64, width, height float64) (point, bool) {
	type pt [2]float64
	for _, quad := range quads {
		var polygon []pt
		for i := 0; i+1 < len(quad); i += 2 {
			polygon = append(polygon, pt{quad[i], quad[i+1]})
		}
		clips := []struct {
			axis     int
			boundary float64
			lower    bool
		}{{0, 0, true}, {0, width, false}, {1, 0, true}, {1, height, false}}
		for _, clip := range clips {
			var result []pt
			for i, end := range polygon {
				start := polygon[(i-1+len(polygon))%len(polygon)]
				in := func(q pt) bool {
					if clip.lower {
						return q[clip.axis] >= clip.boundary
					}
					return q[clip.axis] <= clip.boundary
				}
				sin, ein := in(start), in(end)
				if sin != ein {
					ratio := (clip.boundary - start[clip.axis]) / (end[clip.axis] - start[clip.axis])
					result = append(result, pt{start[0] + ratio*(end[0]-start[0]), start[1] + ratio*(end[1]-start[1])})
				}
				if ein {
					result = append(result, end)
				}
			}
			polygon = result
			if len(polygon) == 0 {
				break
			}
		}
		if len(polygon) == 0 {
			continue
		}
		sum := 0.0
		for i, p := range polygon {
			prev := polygon[(i-1+len(polygon))%len(polygon)]
			sum += prev[0]*p[1] - p[0]*prev[1]
		}
		if math.Abs(sum)/2 > 1 {
			var x, y float64
			for _, p := range polygon {
				x += p[0]
				y += p[1]
			}
			return point{x / float64(len(polygon)), y / float64(len(polygon))}, true
		}
	}
	return point{}, false
}

func floats(v any) []float64 {
	list := asList(v)
	out := make([]float64, len(list))
	for i, x := range list {
		out[i] = mustNum(x)
	}
	return out
}

func toPoint(v any) point {
	o := asObj(v)
	return point{mustNum(o.Must("x")), mustNum(o.Must("y"))}
}
