package node

import (
	"strings"
	"testing"
)

func TestCLIXMLKeepsNativeErrorsAcrossEveryChunkBoundary(t *testing.T) {
	const open = `<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">`
	const errorRecord = `<S S="Error">Write-Error : OB_MIX_PS_FIRST_x000D__x000A_</S>`
	const progress = `<Obj S="progress" RefId="0"><MS><Obj N="Record"><S N="Activity">working</S></Obj></MS></Obj>`
	for _, test := range []struct{ name, input, want string }{
		{"recorded-order", "#< CLIXML\r\nOB_MIX_NATIVE_MIDDLE \r\nOB_MIX_CONSOLE_LAST\r\n" + open + errorRecord + `</Objs>`, "OB_MIX_NATIVE_MIDDLE \r\nOB_MIX_CONSOLE_LAST\r\nWrite-Error : OB_MIX_PS_FIRST\r\n"},
		{"native-first", "OB_MIX_NATIVE_FIRST \r\n#< CLIXML\r\nOB_MIX_NATIVE_LAST \r\n" + open + errorRecord + `</Objs>`, "OB_MIX_NATIVE_FIRST \r\nOB_MIX_NATIVE_LAST \r\nWrite-Error : OB_MIX_PS_FIRST\r\n"},
		{"between-records", "#< CLIXML\n" + open + errorRecord + "native & <angle>\r\n" + errorRecord + `</Objs>` + "after\n", "Write-Error : OB_MIX_PS_FIRST\r\nnative & <angle>\r\nWrite-Error : OB_MIX_PS_FIRST\r\nafter\n"},
		{"plain", "plain <Objs> XML & \"quotes\"\r\n", "plain <Objs> XML & \"quotes\"\r\n"},
		{"progress", "#< CLIXML\r\n" + open + progress + "native\n" + errorRecord + `</Objs>`, "native\nWrite-Error : OB_MIX_PS_FIRST\r\n"},
		{"warning-unicode", "#< CLIXML\n" + open + `<S S="Warning">中文 &lt;x&gt;_x000A_</S></Objs>`, "WARNING: 中文 <x>\n"},
		{"repeated-documents", "#< CLIXML\n" + open + errorRecord + `</Objs>` + "raw\n#< CLIXML\n" + open + errorRecord + `</Objs>`, "Write-Error : OB_MIX_PS_FIRST\r\nraw\nWrite-Error : OB_MIX_PS_FIRST\r\n"},
		{"incomplete-header", "prefix#< CLI", "prefix#< CLI"},
		{"incomplete-record", "#< CLIXML\n" + open + `<S S="Error">unfinished`, `<S S="Error">unfinished`},
		{"native-record-before-root", "#< CLIXML\n" + `<S S="Error">native literal</S>` + open + errorRecord + `</Objs>`, `<S S="Error">native literal</S>` + "Write-Error : OB_MIX_PS_FIRST\r\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := plainErrors(test.input); got != test.want {
				t.Fatalf("exec: got %q want %q", got, test.want)
			}
			for _, size := range []int{1, 2, 3, 7, 13, 256, len(test.input)} {
				var out strings.Builder
				w := clixmlWriter{out: &out}
				for start := 0; start < len(test.input); start += size {
					part := []byte(test.input[start:min(start+size, len(test.input))])
					if n, err := w.Write(part); n != len(part) || err != nil {
						t.Fatalf("stream write: %d %v", n, err)
					}
				}
				w.flush()
				if got := out.String(); got != test.want {
					t.Fatalf("job chunk=%d: got %q want %q", size, got, test.want)
				}
			}
		})
	}
}

func TestCLIXMLNativeOutputIsNotHeldUntilAnXMLRecordArrives(t *testing.T) {
	var out strings.Builder
	w := clixmlWriter{out: &out}
	w.Write([]byte("#< CLIXML\r\n"))
	w.Write([]byte("native error\r\n"))
	if out.String() != "native error\r\n" || len(w.buf) != 0 {
		t.Fatalf("native output was delayed: %q buffered=%d", out.String(), len(w.buf))
	}
}

func TestCLIXMLBoundsIncompleteRecordsWithoutDroppingPlainText(t *testing.T) {
	var out strings.Builder
	w := clixmlWriter{out: &out}
	w.Write([]byte("#< CLIXML\n<Objs>"))
	text := `<S S="Error">` + strings.Repeat("x", 2<<20)
	for start := 0; start < len(text); start += 4096 {
		w.Write([]byte(text[start:min(start+4096, len(text))]))
		if len(w.buf) > 1<<20 {
			t.Fatalf("incomplete record buffer grew to %d", len(w.buf))
		}
	}
	w.flush()
	if out.String() != text {
		t.Fatalf("incomplete/oversized text lost: got %d want %d bytes", out.Len(), len(text))
	}
}
