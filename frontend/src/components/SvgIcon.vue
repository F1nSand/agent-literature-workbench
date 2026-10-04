<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 18 })

const paths: Record<string, string[]> = {
  overview: ['M3 3h8v8H3z', 'M13 3h8v5h-8z', 'M13 10h8v11h-8z', 'M3 13h8v8H3z'],
  library: ['M5 3.5h11l3 3V20a.5.5 0 0 1-.5.5h-13a.5.5 0 0 1-.5-.5z', 'M15.5 3.5v4h4', 'M8 12h8', 'M8 16h8'],
  note: ['M13 4H5a2 2 0 0 0-2 2v13l4-2h8a2 2 0 0 0 2-2v-3', 'm12 8 5-5 3 3-5 5-4 1z', 'm16 4 3 3'],
  assistant: ['M12 3v3', 'M12 18v3', 'M3 12h3', 'M18 12h3', 'm5.6 5.6 2.1 2.1', 'm16.3 16.3 2.1 2.1', 'm18.4 5.6-2.1 2.1', 'm7.7 16.3-2.1 2.1', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z'],
  settings: ['M12 3.5 14 4l1.1 2.1 2-.3 1.4 1.4-.3 2L20.3 11l.2 2-2.1 1.1.3 2-1.4 1.4-2-.3-1.1 2.1-2 .2-1.1-2.1-2 .3-1.4-1.4.3-2L4 13l-.2-2 2.1-1.1-.3-2L7 6.5l2 .3L10 4.7z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z'],
  history: ['M3 12a9 9 0 1 0 2.6-6.4L3 8', 'M3 3v5h5', 'M12 7v5l3 2'],
  plus: ['M12 5v14', 'M5 12h14'],
  close: ['M6 6l12 12', 'M18 6 6 18'],
  minimize: ['M5 12h14'],
  arrow: ['M5 12h14', 'm13 6 6 6-6 6'],
  search: ['m20 20-4.5-4.5', 'M10.8 17a6.2 6.2 0 1 0 0-12.4 6.2 6.2 0 0 0 0 12.4Z'],
  book: ['M4 4.5A2.5 2.5 0 0 1 6.5 2H20v18H6.5A2.5 2.5 0 0 0 4 22z', 'M4 4.5v15A2.5 2.5 0 0 1 6.5 17H20', 'M8 6h8', 'M8 10h8'],
  upload: ['M12 16V4', 'm7 9 5-5 5 5', 'M4 17v3h16v-3'],
  sparkle: ['m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', 'm19 15 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z'],
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <path v-for="(path, index) in paths[name] || paths.book" :key="index" :d="path" />
  </svg>
</template>
