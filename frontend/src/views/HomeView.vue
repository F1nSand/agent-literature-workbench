<script setup lang="ts">
import { useWorkspaceChromeStore } from '../stores/workspaceChrome'
import SvgIcon from '../components/SvgIcon.vue'
const chrome = useWorkspaceChromeStore()
</script>

<template>
  <div class="page-content home-page revised-home">
    <section class="welcome-row">
      <div>
        <h1>从原文出发，<br /><em>把阅读留在手边。</em></h1>
        <p class="welcome-description">在文献库中打开论文，随时记录想法，也可以让助手结合当前阅读位置寻找依据。</p>
      </div>
      <div class="hero-seal" aria-hidden="true"><SvgIcon name="book" :size="30" /></div>
    </section>

    <section class="revised-home-entry">
      <RouterLink class="home-library-entry home-entry-arrive" to="/documents">
        <span class="home-entry-icon"><SvgIcon name="library" :size="24" /></span>
        <strong>文献库</strong><p>查找、整理并打开你的论文原文。</p><span class="home-entry-arrow">进入文献库 <SvgIcon name="arrow" :size="16" /></span>
      </RouterLink>
      <div class="home-tools-entry home-entry-arrive home-entry-arrive-tools">
        <strong>研读工具</strong><p>悬浮窗口会在切换页面后继续保留。</p>
        <div><button @click="chrome.show('notes')"><SvgIcon name="note" :size="17" />打开阅读笔记 <b>↗</b></button><button @click="chrome.show('assistant')"><SvgIcon name="assistant" :size="17" />打开研读助手 <b>↗</b></button></div>
      </div>
    </section>

    <section class="home-reading-note">
      <div><h2>让每个结论<br />都能回到原文。</h2></div>
      <p>笔记会自动保存并形成版本。助手提出的修改先由你预览，采纳后才成为新的笔记版本。</p>
      <span aria-hidden="true">“</span>
    </section>
  </div>
</template>
