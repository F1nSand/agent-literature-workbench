<script setup lang="ts">
import SvgIcon from '../components/SvgIcon.vue'
import { useSurfaceMotion } from '../composables/useSurfaceMotion'
const { enabled: motionEnabled, reduced: motionReduced, setEnabled: setMotionEnabled } = useSurfaceMotion()
defineProps<{
  title: string
  eyebrow: string
  description: string
  emptyTitle: string
  emptyDescription: string
  icon: string
  isSettings?: boolean
}>()
</script>

<template>
  <div class="page-content stage-page">
    <h1>{{ title }}</h1>
    <p class="welcome-description">{{ description }}</p>
    <section class="panel empty-panel">
      <div class="empty-illustration"><SvgIcon :name="icon" :size="28" /></div>
      <h2>{{ emptyTitle }}</h2>
      <p>{{ emptyDescription }}</p>
      <RouterLink class="stage-home-link" to="/">返回工作台概览 <span>→</span></RouterLink>
    </section>
    <section v-if="isSettings" class="settings-motion-panel" aria-labelledby="motion-heading">
      <div class="settings-motion-row">
        <div>
          <h2 id="motion-heading">界面动效</h2>
          <p>{{ motionReduced ? '系统已启用减少动态效果，工作台会保持静止；本机偏好仍会保存，系统设置优先。' : '控制背景光线、指针回应与界面过渡。此偏好只保存在本机。' }}</p>
        </div>
        <button class="motion-toggle" type="button" :aria-pressed="motionEnabled" :aria-label="motionReduced ? `系统减少动态效果已生效；本机偏好${motionEnabled ? '已开启' : '已暂停'}` : motionEnabled ? '关闭界面动效' : '开启界面动效'" @click="setMotionEnabled(!motionEnabled)"><i aria-hidden="true"></i><span>{{ motionReduced ? `系统静止 · ${motionEnabled ? '偏好开' : '偏好关'}` : motionEnabled ? '已开启' : '已暂停' }}</span></button>
      </div>
    </section>
  </div>
</template>
