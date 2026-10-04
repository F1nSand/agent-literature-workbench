import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

type HealthStatus = 'checking' | 'ready' | 'unavailable'

export const useHealthStore = defineStore('health', () => {
  const status = ref<HealthStatus>('checking')
  const message = computed(() => {
    if (status.value === 'unavailable') return '暂时无法连接文献库，已保存的资料不会丢失。'
    return ''
  })

  async function refresh() {
    status.value = 'checking'
    try {
      const response = await fetch('/api/v1/health/ready', { headers: { Accept: 'application/json' } })
      if (!response.ok) throw new Error('unavailable')
      status.value = 'ready'
    } catch {
      status.value = 'unavailable'
    }
  }

  return { status, message, refresh }
})
