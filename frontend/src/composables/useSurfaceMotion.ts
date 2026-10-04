import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const STORAGE_KEY = 'research-desk:surface-motion'
const enabled = ref(true)
const reduced = ref(false)
let users = 0
let media: MediaQueryList | undefined
let frame = 0
let pointer: { x: number; y: number } | undefined
let pointerTarget: EventTarget | null = null
let activeTiltCard: HTMLElement | undefined
let scroll = 0
let pointerQuery: MediaQueryList | undefined
let storageListener: ((event: StorageEvent) => void) | undefined

function syncRoot() {
  if (typeof document === 'undefined') return
  document.documentElement.classList.toggle('surface-motion-enabled', enabled.value)
  document.documentElement.classList.toggle('surface-motion-reduced', reduced.value)
  if (document.hidden) document.documentElement.classList.add('surface-motion-paused')
  else document.documentElement.classList.remove('surface-motion-paused')
}

function scheduleUpdate() {
  if (frame || !enabled.value || reduced.value || document.hidden) return
  frame = requestAnimationFrame(() => {
    frame = 0
    const root = document.documentElement
    if (pointer && pointerQuery?.matches) {
      root.style.setProperty('--surface-pointer-x', `${pointer.x}px`)
      root.style.setProperty('--surface-pointer-y', `${pointer.y}px`)
      root.style.setProperty('--surface-pointer-xp', `${(pointer.x / Math.max(window.innerWidth, 1)) * 100}%`)
      root.style.setProperty('--surface-pointer-yp', `${(pointer.y / Math.max(window.innerHeight, 1)) * 100}%`)
      root.style.setProperty('--surface-pointer-dx', `${Math.max(-14, Math.min(14, (pointer.x / Math.max(window.innerWidth, 1) - .5) * 24))}px`)
      root.style.setProperty('--surface-pointer-dy', `${Math.max(-10, Math.min(10, (pointer.y / Math.max(window.innerHeight, 1) - .5) * 18))}px`)
    }
    root.style.setProperty('--surface-scroll-shift', `${Math.max(-14, Math.min(14, scroll * -0.018))}px`)
    const target = pointerTarget instanceof Element ? pointerTarget.closest<HTMLElement>('.home-library-entry') ?? undefined : undefined
    if (target !== activeTiltCard) {
      activeTiltCard?.style.removeProperty('--card-tilt-x')
      activeTiltCard?.style.removeProperty('--card-tilt-y')
      activeTiltCard = target
    }
    if (activeTiltCard && pointer) {
      const bounds = activeTiltCard.getBoundingClientRect()
      if (bounds.width && bounds.height) {
        const horizontal = (pointer.x - bounds.left) / bounds.width - .5
        const vertical = (pointer.y - bounds.top) / bounds.height - .5
        activeTiltCard.style.setProperty('--card-tilt-y', `${Math.max(-.8, Math.min(.8, horizontal * 1.6))}deg`)
        activeTiltCard.style.setProperty('--card-tilt-x', `${Math.max(-.8, Math.min(.8, vertical * -1.6))}deg`)
      }
    }
  })
}

function clearTilt() {
  activeTiltCard?.style.removeProperty('--card-tilt-x')
  activeTiltCard?.style.removeProperty('--card-tilt-y')
  activeTiltCard = undefined
}

function onPointerMove(event: PointerEvent) {
  if (!pointerQuery?.matches) return
  if (event.pointerType && event.pointerType !== 'mouse' && event.pointerType !== 'pen') return
  pointer = { x: event.clientX, y: event.clientY }
  pointerTarget = event.target
  scheduleUpdate()
}

function onScroll(event?: Event) {
  const target = event?.target
  scroll = target instanceof Element
    ? target.scrollTop
    : window.scrollY || document.documentElement.scrollTop || 0
  scheduleUpdate()
}

function onVisibility() {
  syncRoot()
  if (document.hidden && frame) cancelAnimationFrame(frame)
  if (document.hidden) { frame = 0; clearTilt() }
}

function onReducedChange(event: MediaQueryListEvent) {
  reduced.value = event.matches
  syncRoot()
  if (event.matches && frame) cancelAnimationFrame(frame)
  if (event.matches) { frame = 0; clearTilt() }
}

function initialize() {
  if (typeof window === 'undefined' || users++ > 0) return
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'off' || stored === 'on') enabled.value = stored === 'on'
  } catch { /* Storage can be disabled; session preference remains available. */ }
  media = window.matchMedia('(prefers-reduced-motion: reduce)')
  pointerQuery = window.matchMedia('(hover: hover) and (pointer: fine)')
  reduced.value = media.matches
  media.addEventListener('change', onReducedChange)
  window.addEventListener('pointermove', onPointerMove, { passive: true })
  document.addEventListener('scroll', onScroll, { passive: true, capture: true })
  document.addEventListener('visibilitychange', onVisibility)
  storageListener = (event) => {
    if (event.key === STORAGE_KEY && (event.newValue === 'off' || event.newValue === 'on')) {
      enabled.value = event.newValue === 'on'
      syncRoot()
    }
  }
  window.addEventListener('storage', storageListener)
  syncRoot()
  onScroll()
}

function dispose() {
  if (typeof window === 'undefined' || --users > 0) return
  media?.removeEventListener('change', onReducedChange)
  window.removeEventListener('pointermove', onPointerMove)
  document.removeEventListener('scroll', onScroll, true)
  document.removeEventListener('visibilitychange', onVisibility)
  if (storageListener) window.removeEventListener('storage', storageListener)
  if (frame) cancelAnimationFrame(frame)
  frame = 0
  document.documentElement.classList.remove('surface-motion-enabled', 'surface-motion-reduced', 'surface-motion-paused')
}

function setEnabled(value: boolean) {
  enabled.value = value
  try { localStorage.setItem(STORAGE_KEY, value ? 'on' : 'off') } catch { /* Keep the in-memory setting when storage is unavailable. */ }
  syncRoot()
  if (!value && frame) cancelAnimationFrame(frame)
  if (!value) { frame = 0; clearTilt() }
  else scheduleUpdate()
}

export function useSurfaceMotion() {
  const isStatic = computed(() => reduced.value || !enabled.value)
  onMounted(initialize)
  onBeforeUnmount(dispose)
  return { enabled, reduced, isStatic, setEnabled }
}
