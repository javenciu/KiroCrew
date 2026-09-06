/**
 * usePointerDrag — a drag must terminate on EVERY capture-ending path.
 *
 * The hook's contract is capture-based dragging (setPointerCapture on
 * pointer-down, per its own doc comment "survives the pointer leaving the
 * element bounds (capture)"). The Pointer Events spec delivers
 * `lostpointercapture` as the terminal event when capture ends for any
 * reason: explicit release, a capture steal by another element, or a
 * browser-initiated cancellation. If the hook ignores it, a drag whose
 * capture dies mid-gesture never fires onEnd, and consumer onStart side
 * effects stay stuck while the component remains mounted — unmount guards
 * never run. Several resizer consumers set `document.body.style.userSelect =
 * 'none'` in onStart (one stranded drag makes the whole page unselectable
 * until some later drag happens to clean it up, #8271's symptom); others
 * pin body.cursor page-wide or strand teardown-critical dragging flags.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import React from 'react'
import { usePointerDrag, type PointerDragOptions } from './usePointerDrag'

function Handle(props: PointerDragOptions) {
  const drag = usePointerDrag(props)
  return <div data-testid="drag-handle" {...drag} />
}

function renderHandle(props: PointerDragOptions) {
  const utils = render(<Handle {...props} />)
  return { ...utils, handle: utils.getByTestId('drag-handle') }
}

describe('usePointerDrag capture-loss termination', () => {
  it('fires onEnd when pointer capture is lost mid-drag (the stuck-drag path)', () => {
    const onStart = vi.fn()
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onStart, onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    expect(onStart).toHaveBeenCalledTimes(1)

    // Capture dies without a pointerup/pointercancel ever reaching the
    // element (capture steal / browser cancellation). lostpointercapture is
    // the only notification the element gets.
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('reports `committed` on a capture-loss end exactly like a normal end', () => {
    const onEnd = vi.fn()
    // threshold 0 commits immediately on pointer-down.
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({ committed: true })
  })

  it('normal pointerup still ends the drag exactly once', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 12, clientY: 12 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('does not double-fire onEnd when lostpointercapture follows a normal release', () => {
    // Browsers fire lostpointercapture after EVERY capture release, including
    // the releasePointerCapture the hook itself performs in a normal end —
    // the end path must stay idempotent per drag.
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 12, clientY: 12 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('ignores a stray lostpointercapture with no active drag', () => {
    const onStart = vi.fn()
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onStart, onMove: () => {}, onEnd })

    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onStart).not.toHaveBeenCalled()
    expect(onEnd).not.toHaveBeenCalled()
  })

  it('a new drag after a capture-loss end works from a clean slate', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })
    expect(onEnd).toHaveBeenCalledTimes(1)

    fireEvent.pointerDown(handle, { pointerId: 2, clientX: 30, clientY: 30 })
    fireEvent.pointerUp(handle, { pointerId: 2, clientX: 35, clientY: 30 })
    expect(onEnd).toHaveBeenCalledTimes(2)
  })
})
