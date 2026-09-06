// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { MAX_BLOCKQUOTE_DEPTH } from '../utils/clampNestingDepth'

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

  it('survives deep nested-list nesting (non-blockquote nesting vector)', () => {
    // Each two-space indent level nests one deeper. Indent grows per line, so
    // input size is quadratic in depth -- 1,500 levels (~2.3MB) keeps the test
    // fast while sitting far past the clamp bound (~128 effective levels).
    const lines: string[] = []
    for (let i = 0; i < 1_500; i++) lines.push(`${'  '.repeat(i)}- x`)
    const { container } = render(<MarkdownRenderer content={lines.join('\n')} />)
    expect(container.textContent).toContain('x')
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
})
