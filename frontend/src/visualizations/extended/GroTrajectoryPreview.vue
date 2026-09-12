<template><BoundedGeometryPreview :file="file" :plugin="plugin" mode="gro-trajectory" /></template>
<script setup lang="ts">
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import BoundedGeometryPreview from './BoundedGeometryPreview.vue';
defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
</script>
