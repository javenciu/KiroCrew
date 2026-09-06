/**
 * Input-side markdown nesting clamp.
 *
 * Chat messages are input-controlled markdown. Deeply nested constructs --
 * blockquote marker runs (`>>>>…` / `> > > …`) and progressively indented list
 * items -- parse into a tree whose depth equals the nesting count. Recursive
 * layers downstream (remark-rehype's mdast->hast transform first, then this
 * module's own tree walkers) recurse to that depth and throw
 * `RangeError: Maximum call stack size exceeded` while rendering a single
 * message. Measured on this codebase: micromark parse survives a 5,000-deep
 * blockquote run in ~70ms, but the mdast->hast transform overflows the stack --
 * so no per-walker guard inside our code can reach the crash. The only fix
 * shape that covers every recursive layer at once is clamping nesting depth in
 * the SOURCE STRING before it is parsed, at the single choke point where
 * message markdown enters the renderer.
 *
 * Semantics below the bounds are untouched: a line is rewritten only when its
 * leading blockquote run exceeds MAX_BLOCKQUOTE_DEPTH markers or a list item's
 * leading indent exceeds MAX_LIST_INDENT_COLS characters -- shapes that occur
 * in practice only as pathological or adversarial input. Content is preserved
 * (the payload renders at the clamp depth); nothing is dropped.
 *
 * Fenced code blocks are exempt: marker runs inside ``` / ~~~ fences are
 * literal text, not nesting. The standard for "is this line a fence?" is
 * MICROMARK's CommonMark semantics (spec 4.5), not `fixCodeFences`' looser
 * line shape: in particular a backtick fence's info string may not contain a
 * backtick, so such a line is a paragraph to the parser and must NOT open an
 * exemption window here. Where this pass is unsure it fails CLOSED (does not
 * enter fence state): the worst case is cosmetic clamping of marker art the
 * parser would have treated as fenced, never an unclamped deep run reaching
 * the parser.
 */

/** Maximum retained blockquote nesting depth (markers per line). */
export const MAX_BLOCKQUOTE_DEPTH = 100

/** Maximum retained leading indent (chars) for a list-item line. With 2-space
 *  indents this allows ~128 genuine nesting levels -- far past anything a
 *  human writes, far below the ~thousands where recursion overflows. */
export const MAX_LIST_INDENT_COLS = 256

/** Leading blockquote marker run: optional 0-3 space indent, then one or more
 *  `>` each optionally followed by one space/tab (CommonMark consumes it). */
const QUOTE_RUN = /^( {0,3})((?:>[ \t]?)+)/

/** List-item marker after an indent: bullet or ordered (CommonMark shapes). */
const LIST_AFTER_INDENT = /^([ \t]+)(?=(?:[-*+]|\d{1,9}[.)])[ \t])/

/** Fence open/close detection, mirroring fixCodeFences. */
const FENCE_LINE = /^ {0,3}(```+|~~~+)/

function clampLine(line: string, maxQuoteDepth: number, maxListIndentCols: number): string {
  let prefix = ''
  let rest = line
  const q = QUOTE_RUN.exec(rest)
  if (q) {
    const run = q[2]
    // Count markers; the run regex scan and this count are both linear.
    let markers = 0
    for (let i = 0; i < run.length; i++) if (run.charCodeAt(i) === 62 /* '>' */) markers++
    if (markers > maxQuoteDepth) {
      prefix = q[1] + '> '.repeat(maxQuoteDepth)
      rest = rest.slice(q[0].length)
    } else {
      prefix = q[0]
      rest = rest.slice(q[0].length)
    }
  }
  // List indent clamp applies to the remainder after any quote prefix, so a
  // moderate quote run cannot smuggle an unbounded list indent behind it.
  const li = LIST_AFTER_INDENT.exec(rest)
  if (li && li[1].length > maxListIndentCols) {
    rest = ' '.repeat(maxListIndentCols) + rest.slice(li[1].length)
  }
  return prefix + rest
}

export function clampNestingDepth(
  s: string,
  maxQuoteDepth: number = MAX_BLOCKQUOTE_DEPTH,
  maxListIndentCols: number = MAX_LIST_INDENT_COLS,
): string {
  // Cheap bail: no single line can exceed either bound if the whole string is
  // shorter than the smaller bound.
  if (s.length <= Math.min(maxQuoteDepth, maxListIndentCols)) return s
  const lines = s.split('\n')
  let inFence = false
  let fenceMarker = ''
  let changed = false
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const fm = FENCE_LINE.exec(line)
    if (fm) {
      const fence = fm[1]
      if (inFence) {
        if (
          fence[0] === fenceMarker[0] &&
          fence.length >= fenceMarker.length &&
          /^[ \t\r]*$/.test(line.slice(line.indexOf(fence) + fence.length))
        ) {
          inFence = false
        }
        // In-fence lines (content or closer) are exempt either way.
        continue
      }
      // Fence OPEN candidate. CommonMark (spec 4.5): the info string of a
      // BACKTICK fence may not contain a backtick -- micromark reads such a
      // line as a paragraph, not a fence. Opening a fence here would exempt
      // every following line from clamping (a guard bypass), so the
      // fail-closed choice is to NOT enter fence state and let the line fall
      // through to the normal clamp path. Tilde fences may carry any info
      // string per spec.
      const info = line.slice(fm[0].length)
      if (!(fence[0] === '`' && info.includes('`'))) {
        inFence = true
        fenceMarker = fence
        continue
      }
      // else: paragraph line to the parser -- fall through, stays clampable.
    }
    if (inFence) continue
    // Fast per-line bail before any regex: a line shorter than the smaller
    // bound cannot exceed either limit.
    if (line.length <= Math.min(maxQuoteDepth, maxListIndentCols)) continue
    const clamped = clampLine(line, maxQuoteDepth, maxListIndentCols)
    if (clamped !== line) {
      lines[i] = clamped
      changed = true
    }
  }
  return changed ? lines.join('\n') : s
}
