// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { MAX_BLOCKQUOTE_DEPTH, MAX_HTML_TAG_DEPTH, clampNestingDepth } from '../utils/clampNestingDepth'

// A chat message is attacker/input-controlled markdown. Deeply nested constructs
// (blockquote runs like ">>>>…", or equivalently deep nested lists) parse into a
// tree whose depth equals the nesting count. The renderer's recursive walkers --
// and the remark/rehype visitors under them -- then recurse to that depth, and past
// the JS engine's call-stack limit a single message throws
// `RangeError: Maximum call stack size exceeded` and takes down the transcript view.
//
// These tests pin the guarantee that pathological nesting depth is CLAMPED before
// it can reach any recursive layer: rendering must complete without throwing, and
// content survives (the message text is still visible, not dropped).

const NEST = (n: number) => '>'.repeat(n) + ' payload-text'

describe('MarkdownRenderer nesting depth clamp', () => {
  it('renders 50-deep nesting normally (below any clamp bound)', () => {
    const { container } = render(<MarkdownRenderer content={NEST(50)} />)
    expect(container.textContent).toContain('payload-text')
    // Genuine nesting below the bound is preserved as real structure.
    expect(container.querySelectorAll('blockquote').length).toBeGreaterThanOrEqual(50)
  })

  it('survives 5,000-deep blockquote nesting without a stack overflow', () => {
    const { container } = render(<MarkdownRenderer content={NEST(5_000)} />)
    expect(container.textContent).toContain('payload-text')
  })

  it('survives 50,000-deep blockquote nesting without a stack overflow', () => {
    const { container } = render(<MarkdownRenderer content={NEST(50_000)} />)
    expect(container.textContent).toContain('payload-text')
  })

  it('a marker-only (empty) list item nests like any other and is clamped', () => {
    // `      -` at end of line is an empty item to CommonMark and nests the
    // same way `      - item` does. The indent clamp must accept end-of-line
    // after the marker, or a progressively indented run of empty items keeps
    // its full indent and builds the deep tree the clamp exists to bound.
    const lines: string[] = []
    for (let i = 0; i < 600; i++) lines.push(' '.repeat(i * 2) + '-')
    const src = lines.join('\n')
    expect(clampNestingDepth(src)).not.toBe(src)
    const { container } = render(<MarkdownRenderer content={src} />)
    expect(container.querySelector('li')).not.toBeNull()
  })

  it('survives deep nested-list nesting (non-blockquote nesting vector)', () => {
    // Each two-space indent level nests one deeper. Indent grows per line, so
    // input size is QUADRATIC in depth: the original 1,500-level (~2.3MB)
    // fixture parsed in ~3s locally but 28s on CI hardware, tripping the 15s
    // test budget -- a fixture-cost timeout, not a clamp miss. 600 levels
    // (~360KB) keeps the same conviction (4.7x past the ~128 effective-level
    // clamp bound) at ~6x less parse work.
    const lines: string[] = []
    for (let i = 0; i < 600; i++) lines.push(`${'  '.repeat(i)}- x`)
    const { container } = render(<MarkdownRenderer content={lines.join('\n')} />)
    expect(container.textContent).toContain('x')
  })

  it('survives a double-spaced blockquote re-open run (marker-run bypass vector)', () => {
    // CommonMark re-opens a blockquote through up to 3 spaces of indent, so
    // `>  >  >  ` nests one level per marker even though the markers are not
    // consecutive characters. A scanner that counts only consecutive `>` chars
    // sees ONE marker per group and lets the run through unclamped -- the
    // exact stack-overflow class through an alternate spelling.
    const { container } = render(
      <MarkdownRenderer content={'>  '.repeat(5_000) + 'payload-dspace'} />,
    )
    expect(container.textContent).toContain('payload-dspace')
  })

  it('survives an interleaved quote/list container prefix (container-class vector)', () => {
    // Containers interleave: `> - > - …` alternates blockquote and list-item
    // units on one line, nesting ~two tree levels per 4 bytes with no indent
    // growth and no long marker run -- invisible to both a quote-run counter
    // and an indent clamp. The unit of clamping must be the CommonMark
    // container, not any one marker shape.
    const { container } = render(
      <MarkdownRenderer content={'> - '.repeat(5_000) + 'payload-interleave'} />,
    )
    expect(container.textContent).toContain('payload-interleave')
  })

  it('preserves a below-bound interleaved prefix byte-identically (no false clamp)', () => {
    // 30 units of `> - ` sit far below the 100-unit bound: the clamp must not
    // rewrite them, and micromark must see genuine nested structure.
    const src = '> - '.repeat(30) + 'shallow-mix'
    expect(clampNestingDepth(src)).toBe(src)
    const { container } = render(<MarkdownRenderer content={src} />)
    expect(container.textContent).toContain('shallow-mix')
    expect(container.querySelectorAll('blockquote').length).toBeGreaterThanOrEqual(30)
  })

  it('clamps by minimal mutation: kept prefix byte-identical, one inserted escape', () => {
    // The rewrite keeps the first MAX_BLOCKQUOTE_DEPTH units verbatim and
    // inserts a single backslash before the next marker, so the remainder is
    // literal text at the clamp depth: nothing dropped, source-position drift
    // on a clamped line bounded to one byte.
    const src = '>'.repeat(MAX_BLOCKQUOTE_DEPTH + 50) + ' tail-payload'
    const out = clampNestingDepth(src)
    expect(out.length).toBe(src.length + 1)
    expect(out.slice(0, MAX_BLOCKQUOTE_DEPTH)).toBe('>'.repeat(MAX_BLOCKQUOTE_DEPTH))
    expect(out.charAt(MAX_BLOCKQUOTE_DEPTH)).toBe('\\')
    expect(out.endsWith(' tail-payload')).toBe(true)
  })

  it('clamp path stays roughly linear in input length (no quadratic re-scan)', () => {
    const t50k = (() => {
      const s = performance.now()
      render(<MarkdownRenderer content={NEST(50_000)} />)
      return performance.now() - s
    })()
    const t100k = (() => {
      const s = performance.now()
      render(<MarkdownRenderer content={NEST(100_000)} />)
      return performance.now() - s
    })()
    // 2x input must not cost anywhere near 4x time; generous 3.5x bound absorbs jitter.
    expect(t100k).toBeLessThan(Math.max(t50k, 5) * 3.5)
  })

  it('a backtick-in-info-string pseudo-fence does not open an exemption window', () => {
    // CommonMark (spec 4.5): a backtick fence's info string may not contain a
    // backtick, so " ```x`y" is a PARAGRAPH to micromark -- not a fence open.
    // If the clamp treated it as a fence, every following line would be exempt
    // and a deep quote run would reach the parser unclamped: the exact crash
    // this guard exists to prevent, reachable through the guard itself.
    const content = ' ```x`y\n' + '>'.repeat(50_000) + ' payload-d1\n'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('payload-d1')
  })

  it('deep marker art inside a genuine closed fence stays byte-identical', () => {
    // The exemption itself, pinned: a properly opened and closed backtick
    // fence protects its content from clamping -- a marker run deeper than
    // MAX_BLOCKQUOTE_DEPTH inside the fence is literal text and must survive
    // untouched (rendered inside a code block, not as blockquotes).
    const art = '>'.repeat(MAX_BLOCKQUOTE_DEPTH + 37) + ' fence-art'
    const content = '```text\n' + art + '\n```\n'
    const { container } = render(<MarkdownRenderer content={content} />)
    // Byte-exact survival: if the clamp had rewritten the line, the marker run
    // would be truncated; if the fence were not honored, the parser would
    // consume the markers as blockquotes. Either way the literal run is gone.
    expect(container.textContent).toContain(art)
    expect(container.querySelector('blockquote')).toBeNull()
  })

  it('a fence marker inside an open html block does not open an exemption window', () => {
    // CommonMark (spec 4.6): a `<div>` line opens a type-6 HTML block that
    // swallows every following line until a BLANK line -- including a
    // ```-shaped line, which is raw block content to micromark, NOT a fence
    // open. If the clamp entered fence state on it, everything after would be
    // exempt: a 3-token shield prefix would carry an unclamped deep tag run
    // straight to parse5 -- the crash class through the guard's own exemption.
    const content = '<div>\n```\n' + '<div>'.repeat(5_000) + 'payload-htmlshield'
    // Scanner half (deterministic in any environment): the shielded fence
    // line must not exempt the deep run -- past-bound opens are neutralized.
    // The line-1 div occupies one stack slot, so exactly
    // 5000 - (MAX_HTML_TAG_DEPTH - 1) opens sit past the bound.
    const clamped = clampNestingDepth(content)
    expect(clamped).not.toBe(content)
    expect((clamped.match(/&lt;div/g) ?? []).length).toBe(5_000 - (MAX_HTML_TAG_DEPTH - 1))
    expect(clamped).toContain('payload-htmlshield')
    // Renderer half: the attack renders with the payload visible (no crash).
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('payload-htmlshield')
  })

  it('a blank line ends the html block: a fence after it is genuine again', () => {
    // Regression control (passes with and without the shield): the type-6
    // block ends at the blank line, so the fence after it IS real to
    // micromark and its content keeps the exemption -- deep marker art inside
    // stays byte-identical. Pins that the shield latch CLEARS.
    const art = '>'.repeat(MAX_BLOCKQUOTE_DEPTH + 37) + ' shield-art'
    const src = '<div>\n\n```text\n' + art + '\n```\n'
    expect(clampNestingDepth(src)).toBe(src)
    const { container } = render(<MarkdownRenderer content={src} />)
    expect(container.textContent).toContain(art)
  })

  it('blank lines do not end a <pre> block: the shield persists to the textual closer', () => {
    // Type-1 blocks (`<pre`/`<script`/`<style`/`<textarea`) end at a TEXTUAL
    // closer line, not at a blank -- CommonMark 4.6's end-condition table. A
    // latch that cleared at any blank line would re-open the fence bypass
    // through a `<pre>` + blank + ``` prefix (the blank-crossing sibling of
    // the same class). parse5 parses children inside <pre> normally, so the
    // deep run still nests without the clamp.
    const content = '<pre>\n\n```\n' + '<div>'.repeat(5_000) + 'payload-preshield'
    const clamped = clampNestingDepth(content)
    expect(clamped).not.toBe(content)
    expect((clamped.match(/&lt;div/g) ?? []).length).toBe(5_000 - (MAX_HTML_TAG_DEPTH - 1))
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('payload-preshield')
  })

  it('a same-line closer ends the block: <pre>x</pre> then a fence is genuine', () => {
    // Regression control (passes both sides): the end condition may match on
    // the start line itself, so `<pre>x</pre>` is a single-line block and the
    // fence on the next line is real -- deep tag art inside it must survive
    // byte-identical (both the fence pass and the HTML pass must honor it).
    const art = '<div>'.repeat(MAX_HTML_TAG_DEPTH + 37) + ' pre-art'
    const src = '<pre>x</pre>\n```text\n' + art + '\n```\n'
    expect(clampNestingDepth(src)).toBe(src)
    const { container } = render(<MarkdownRenderer content={src} />)
    expect(container.textContent).toContain(art)
  })

  it('a container-nested html block still shields an indented fence line', () => {
    // Html blocks open inside list items and quotes: `- <div>` opens the
    // block after the container prefix, and an indented ``` line is list-item
    // continuation -- still block content, still not a fence. Latch detection
    // must strip the container run first or this spelling re-opens the bypass
    // with two extra bytes.
    const content = '- <div>\n  ```\n  ' + '<div>'.repeat(5_000) + 'payload-lishield'
    const clamped = clampNestingDepth(content)
    expect(clamped).not.toBe(content)
    expect((clamped.match(/&lt;div/g) ?? []).length).toBe(5_000 - (MAX_HTML_TAG_DEPTH - 1))
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('payload-lishield')
  })

  it('a comment block shields across blank lines until its textual closer', () => {
    // Type-2 blocks (`<!--`) end at a line containing `-->`, NOT at a blank
    // line -- a blank-only latch would re-open the bypass through a comment
    // prefix. The fence-shaped line inside the comment is raw content; the
    // deep run AFTER the closer is a fresh block and must be clamped.
    const content = '<!--\n\n```\n-->\n' + '<div>'.repeat(5_000) + 'payload-commentshield'
    const clamped = clampNestingDepth(content)
    expect(clamped).not.toBe(content)
    expect((clamped.match(/&lt;div/g) ?? []).length).toBe(5_000 - MAX_HTML_TAG_DEPTH)
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('payload-commentshield')
  })

  it('a complete non-listed tag alone on a line shields the next fence line (condition 7)', () => {
    // CommonMark 4.6 condition 7: ANY complete tag alone on a line opens an
    // html block ending at a blank line -- the shield does not require a
    // known block name. `<x-widget>` + ``` + deep run is the same bypass
    // through a custom element.
    const content = '<x-widget>\n```\n' + '<div>'.repeat(5_000) + 'payload-anytagshield'
    const clamped = clampNestingDepth(content)
    expect(clamped).not.toBe(content)
    expect((clamped.match(/&lt;div/g) ?? []).length).toBe(5_000 - (MAX_HTML_TAG_DEPTH - 1))
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('payload-anytagshield')
  })
})

describe('MarkdownRenderer raw-HTML tag-nesting clamp', () => {
  // Raw HTML is the third spelling of the same crash class: `rehype-raw`
  // (wired into both rehype pipelines) parses embedded HTML into HAST whose
  // depth equals the tag-nesting count, and the parse5->HAST transform plus
  // the module's recursive walkers recurse to that depth. The container pass
  // never touches tags, so without the HTML pass a `<div>` run reproduces the
  // identical RangeError through an ordinary input-controlled message.
  // Fails-without-fix PROVEN: at the unfixed tree this exact fixture throws
  // `RangeError: Maximum call stack size exceeded` inside
  // hast-util-from-parse5 (~2.2s); with the fix it renders in ~54ms.
  it('survives 5,000-deep raw <div> nesting without a stack overflow', () => {
    const { container } = render(
      <MarkdownRenderer content={'<div>'.repeat(5_000) + 'payload-html'} />,
    )
    expect(container.textContent).toContain('payload-html')
  })

  it('survives self-closing-spelled non-void nesting (`<div/>` still opens per parse5)', () => {
    // HTML ignores the trailing slash on a non-void element, so `<div/>` runs
    // nest exactly like `<div>` runs. A model that honored the self-closing
    // spelling would under-count to zero and let the run through -- the same
    // alternate-spelling bypass class as the `>  >  >` container vector.
    const { container } = render(
      <MarkdownRenderer content={'<div/>'.repeat(5_000) + 'payload-selfclose'} />,
    )
    expect(container.textContent).toContain('payload-selfclose')
  })

  it('bogus close tags do not drain the depth counter (`</span>` under a div run)', () => {
    // parse5's recovery ignores a close tag that matches nothing open, so the
    // real stack keeps growing. A model that popped on ANY close would be
    // drained to zero by interleaved bogus closers while true depth explodes.
    const { container } = render(
      <MarkdownRenderer content={'<div></span>'.repeat(5_000) + 'payload-bogusclose'} />,
    )
    expect(container.textContent).toContain('payload-bogusclose')
  })

  it('multi-line tags count toward depth (tag spanning a line break)', () => {
    // A start tag may break lines inside its attribute region; the scanner
    // must latch "in tag" across lines and still count the open, or a
    // line-spanning spelling escapes the model. Kept below the render
    // depth that would slow the suite: conviction is that clamping engaged.
    const lines = Array(MAX_HTML_TAG_DEPTH + 60).fill('<div\nclass="x">').join('')
    const out = clampNestingDepth(lines)
    expect(out).toContain('&lt;div')
  })

  it('sequential paired HTML stays byte-identical (no false clamp on oscillation)', () => {
    // Depth oscillates 0->1->0 across well-formed pairs: the model must pop
    // on exact-match closes so ordinary long documents are never rewritten.
    const src = '<div>x</div>'.repeat(2_000)
    expect(clampNestingDepth(src)).toBe(src)
  })

  it('same-name implied-end siblings stay flat (`<li>` spam is not nesting)', () => {
    // parse5 closes an open <li> when the next <li> starts, so 500 of them
    // sit at depth <=2, not 500. The implied-end set must replace the stack
    // top, or every real-world markdown list rendered as HTML gets mangled.
    const src = '<ul>' + '<li>item'.repeat(500) + '</ul>'
    expect(clampNestingDepth(src)).toBe(src)
  })

  it('void elements never count toward depth (`<br>` spam untouched)', () => {
    const src = '<br>'.repeat(2_000) + 'end-voids'
    expect(clampNestingDepth(src)).toBe(src)
  })

  it('a close tag inside a quoted attribute value does not pop (attr-data fake close)', () => {
    // `<div data-x="</div>">` opens ONE div: the quoted value is attribute
    // data, not a close tag. A scanner that read it as a close would drain
    // one pop per open and never clamp the run.
    const { container } = render(
      <MarkdownRenderer
        content={'<div data-x="</div>">'.repeat(5_000) + 'payload-attrclose'}
      />,
    )
    expect(container.textContent).toContain('payload-attrclose')
  })

  it('a close tag inside an inline code span does not pop (code-span fake close)', () => {
    // `` `</div>` `` is literal text to micromark -- parse5 never sees it, so
    // the real stack keeps every `<div>` open. Popping on masked closes would
    // under-count; the mask must block the pop while opens keep counting.
    const { container } = render(
      <MarkdownRenderer content={'<div>`</div>`'.repeat(5_000) + 'payload-spanclose'} />,
    )
    expect(container.textContent).toContain('payload-spanclose')
  })

  it('deep tag runs inside a genuine closed fence stay byte-identical', () => {
    // Fence exemption covers the HTML pass identically: tags inside a real
    // fence are literal text (never parsed by rehype-raw at all).
    const art = '<div>'.repeat(MAX_HTML_TAG_DEPTH + 37) + ' tag-art'
    const content = '```text\n' + art + '\n```\n'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain(art)
  })

  it('a close tag inside an HTML comment does not pop (comment fake close)', () => {
    // `<div><!-- </div> -->` opens ONE div and adds a comment node: to parse5
    // the `</div>` is comment CONTENT, so the div stays open. A scanner that
    // read it as a close would drain one pop per open and never clamp the run.
    const { container } = render(
      <MarkdownRenderer content={'x ' + '<div><!-- </div> -->'.repeat(5_000)} />,
    )
    expect(container).toBeTruthy()
  })

  it('a close tag inside a PI or a declaration does not pop either', () => {
    // Same node class, two other spellings the tokenizer treats as one node.
    for (const wrap of ['<? </div> ?>', '<! </div> >']) {
      const { container } = render(
        <MarkdownRenderer content={'x ' + ('<div>' + wrap).repeat(5_000)} />,
      )
      expect(container).toBeTruthy()
      cleanup()
    }
  })

  // An opaque span must end exactly where the tokenizer ends it. Ending one
  // LATE skips tags parse5 does nest, which leaves the run unclamped -- the
  // under-count direction. Each spelling below is asserted on the clamp output
  // (deterministic, and a 6,000-tag render per case is needlessly slow); the
  // end-to-end crash is already pinned by the render cases above.
  const DEEP = 6_000
  const clampedOpens = (s: string, re: RegExp = /&lt;div>/g) => (clampNestingDepth(s).match(re) ?? []).length

  it('CDATA is a bogus comment ending at the first `>`, not at `]]>`', () => {
    // The CDATA tokenizer state is reachable only in foreign (SVG/MathML)
    // content. In HTML content `<![CDATA[` is a bogus comment, so parse5 ends
    // it at the first `>` and nests everything after -- including the run.
    expect(clampedOpens('<![CDATA[ x > ' + '<div>'.repeat(DEEP) + ' ]]>')).toBeGreaterThan(0)
    // No explicit `>` of its own: the first `<div>`'s `>` ends the span.
    expect(clampedOpens('<![CDATA[ ' + '<div>'.repeat(DEEP) + ' ]]>')).toBeGreaterThan(0)
  })

  it('a comment also ends at `--!>`', () => {
    expect(clampedOpens('<!-- x --!>' + '<div>'.repeat(DEEP))).toBeGreaterThan(0)
  })

  it('an abrupt empty comment (`<!-->`, `<!--->`) ends immediately', () => {
    expect(clampedOpens('<!-->' + '<div>'.repeat(DEEP))).toBeGreaterThan(0)
    expect(clampedOpens('<!--->' + '<div>'.repeat(DEEP))).toBeGreaterThan(0)
  })

  it('a close tag inside a malformed close (`</ ... >`) does not pop', () => {
    // `</` + a non-name is a bogus comment running to the first `>`, so the
    // `</div>` inside it is content and the `<div>` before it stays open.
    expect(clampedOpens('x ' + '<div></ </div> >'.repeat(DEEP))).toBeGreaterThan(0)
  })

  it('a comment spanning lines keeps swallowing closes until its terminator', () => {
    // The span latches across the newline, so the `</div>` on the second line
    // is still comment content and pops nothing.
    const unit = '<div><!--\n</div>\n-->'
    const { container } = render(<MarkdownRenderer content={'x ' + unit.repeat(2_000)} />)
    expect(container).toBeTruthy()
  })

  it('an ordinary comment below the bound stays byte-identical', () => {
    const src = 'text <!-- a normal note --> more <div>one</div>'
    expect(clampNestingDepth(src)).toBe(src)
  })

  it('a mismatched-length backtick run does not close a cross-line code span', () => {
    // CommonMark closes a span only on a run of EXACTLY the opening length.
    // Here the `` `` `` opener on line 1 is closed by the `` `` `` at the END
    // of line 2, so the single-backtick run before `</div>` is literal and so
    // is the `</div>`: parse5 never sees that close, the div stays open, and a
    // mask that let any run "close" the span would pop it -- one drained pop
    // per open, so the run never clamps.
    expect(clampedOpens('x<div>``\n`</div>``\n'.repeat(DEEP))).toBeGreaterThan(0)
    // Same rule, one line per unit, every model state clean at line end: the
    // `</div>` sits inside the 2-run span, the trailing 1-run span holds a
    // literal 2-run. Under any-run pairing every unit pops and nothing clamps.
    expect(clampedOpens('x<div>`` ` </div> `` ` `` `\n'.repeat(DEEP))).toBeGreaterThan(0)
  })

  it('an unpaired backtick before a deep run does not switch the guard off', () => {
    // An unpaired run is literal text to micromark, so the tags after it are
    // real. The mask may only ever suppress POPS; an open past the bound is
    // neutralized whether or not the mask is on.
    expect(clampedOpens('x' + '<div>'.repeat(MAX_HTML_TAG_DEPTH + 1) + '` ' + '<div>'.repeat(DEEP))).toBeGreaterThan(0)
    expect(clampedOpens('x `\nx ' + '<div>'.repeat(DEEP))).toBeGreaterThan(0)
    // A blank line ends the paragraph, and with it any span that was open.
    expect(clampedOpens('x `\n\nx ' + '<div>'.repeat(DEEP))).toBeGreaterThan(0)
  })

  it('a genuine cross-line code span below the bound stays byte-identical', () => {
    const src = 'a <div>`\n</div> literal` b <div>x</div>'
    expect(clampNestingDepth(src)).toBe(src)
  })

  it('inside an HTML block, parse5-shaped tag names (`<x:y>`) count toward depth', () => {
    // Raw block content is tokenized by parse5, whose tag-name state accepts
    // any byte until whitespace, '/' or '>', so `<x:y>` is a real element
    // there. Reading it with micromark's inline grammar (letters, digits, '-')
    // would leave every such open uncounted and the run unclamped.
    expect(clampedOpens('<div>\n' + '<x:y>'.repeat(DEEP), /&lt;x:y>/g)).toBeGreaterThan(0)
    const { container } = render(<MarkdownRenderer content={'<div>\n' + '<x:y>'.repeat(DEEP)} />)
    expect(container).toBeTruthy()
  })

  it('inside an HTML block, a tag ended by form feed, CR or holding NUL still counts', () => {
    // The HTML tokenizer's whitespace set is tab, LF, FF, CR, space -- wider
    // than CommonMark's -- and NUL is a name byte (substituted, not a stop).
    // A tag whose name is ended by FF is a real element to parse5, so the
    // terminator check must accept the tokenizer's set in raw context, or the
    // open is read as literal text and never counted.
    for (const tag of ['<span\f>', '<span\r>', '<span\0>']) {
      const src = '<div>\n' + tag.repeat(DEEP)
      expect(clampNestingDepth(src)).not.toBe(src)
    }
  })

  it('outside an HTML block, `<x:y>` is literal text and stays byte-identical', () => {
    // Micromark's inline-HTML grammar does not accept ':' in a tag name, so
    // parse5 never sees a tag here; nothing to count, nothing to rewrite.
    const src = 'x ' + '<x:y>'.repeat(DEEP)
    expect(clampNestingDepth(src)).toBe(src)
  })

  it('clamps by minimal mutation: below-bound tags byte-identical, `&lt;` past bound', () => {
    const src = '<div>'.repeat(MAX_HTML_TAG_DEPTH + 50) + 'tail-html'
    const out = clampNestingDepth(src)
    // First MAX_HTML_TAG_DEPTH opens are untouched…
    expect(out.slice(0, MAX_HTML_TAG_DEPTH * 5)).toBe('<div>'.repeat(MAX_HTML_TAG_DEPTH))
    // …every subsequent open is neutralized to literal text, payload intact.
    expect(out.slice(MAX_HTML_TAG_DEPTH * 5)).toBe('&lt;div>'.repeat(50) + 'tail-html')
  })
})

