<template>
  <SqlDump v-if="plugin.reader === 'sql-dump'" :file="file" :plugin="plugin" />
  <PostgresDump v-else-if="plugin.reader === 'pg-dump'" :file="file" :plugin="plugin" />
  <p v-else role="alert">未注册的数据库转储读取器。</p>
</template>
<script setup lang="ts">
import {defineAsyncComponent} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const SqlDump=defineAsyncComponent(()=>import('./SQLDumpPreview.vue'));
const PostgresDump=defineAsyncComponent(()=>import('./PostgresDumpPreview.vue'));
</script>
