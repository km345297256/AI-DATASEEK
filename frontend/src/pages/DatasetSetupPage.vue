<template>
  <div
    class="min-h-[100dvh] bg-[var(--background-gray-main)] px-4 py-8 text-[var(--text-primary)] sm:px-6 sm:py-12"
  >
    <main class="mx-auto w-full max-w-2xl">
      <button
        type="button"
        class="mb-6 inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--fill-tsp-gray-main)] hover:text-[var(--text-primary)]"
        @click="router.push('/datasets')"
      >
        <ArrowLeft class="size-3.5" />
        返回数据集管理
      </button>

      <header class="mb-7">
        <div
          class="mb-4 inline-flex items-center gap-2 rounded-full border border-[#b7d1c5] bg-[#edf6f1] px-3 py-1.5 text-xs font-medium text-[#226b51] dark:border-[#3f6453] dark:bg-[#21372d] dark:text-[#9bd0b8]"
        >
          <Database class="size-3.5" />
          科学数据探查 · 数据设置
        </div>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">添加待探测数据集</h1>
        <p class="mt-2 max-w-xl text-sm leading-6 text-[var(--text-secondary)]">
          填写数据集信息和本机绝对路径。提交后自动保存到数据集列表并进入探查，以后可在「本地数据」中直接打开。
        </p>
      </header>

      <form
        class="overflow-hidden rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-sm"
        autocomplete="off"
        @submit.prevent="submit"
      >
        <section class="space-y-5 p-5 sm:p-7">
          <label class="field-label">
            <span>数据集名称 <span class="required-mark">*</span></span>
            <input
              v-model.trim="name"
              name="name"
              class="form-field"
              type="text"
              required
              maxlength="300"
              placeholder="例如：祁连山降水栅格数据集"
            />
          </label>

          <label class="field-label">
            <span>数据集简介 <span class="required-mark">*</span></span>
            <textarea
              v-model.trim="description"
              name="description"
              class="form-field min-h-32 resize-y leading-6"
              required
              maxlength="4000"
              placeholder="简要说明数据内容、时空范围、数据格式和期望的探测方向"
            />
          </label>

          <label class="field-label">
            <span>所属领域</span>
            <select v-model="domain" class="form-field" :disabled="!presets.length" required>
              <option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.name }}</option>
            </select>
            <span v-if="presetError" role="alert" class="field-hint">领域加载失败。<button type="button" class="underline" @click="loadPresets">重新加载</button></span>
          </label>

          <label class="field-label">
            <span class="flex items-center gap-2">
              <FolderKey class="size-4 text-[#2b7659]" />
              数据存储绝对路径 <span class="required-mark">*</span>
            </span>
            <input
              v-model="storageDirectory"
              name="storage_directory"
              class="form-field font-mono text-xs"
              type="text"
              required
              maxlength="4096"
              spellcheck="false"
              placeholder="/data/datasets/example"
            />
            <span class="field-hint">
              路径必须是允许目录内的本机绝对路径；数据仅以只读方式挂载。
            </span>
          </label>

          <div
            class="flex items-start gap-2.5 rounded-lg border border-[#c6dbd1] bg-[#f3f8f5] px-3.5 py-3 text-xs leading-5 text-[#315f4c] dark:border-[#405f50] dark:bg-[#22332b] dark:text-[#a9cbbb]"
          >
            <ShieldCheck class="mt-0.5 size-4 shrink-0" />
            <span>
              真实路径只通过本次提交发送，不会写入 URL 或浏览器存储，也不会在数据集页面中返回。
            </span>
          </div>
        </section>

        <footer
          class="flex flex-col-reverse gap-3 border-t border-[var(--border-main)] bg-[var(--background-gray-main)] px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-7"
        >
          <p class="text-xs text-[var(--text-tertiary)]"><span class="required-mark">*</span> 为必填项</p>
          <button type="submit" class="submit-action" :disabled="submitting || !presets.length">
            <LoaderCircle v-if="submitting" class="size-4 animate-spin" />
            <ArrowRight v-else class="size-4" />
            {{ submitting ? '正在保存数据集…' : '保存并开始探查' }}
          </button>
        </footer>
      </form>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ArrowLeft,
  ArrowRight,
  Database,
  FolderKey,
  LoaderCircle,
  ShieldCheck,
} from 'lucide-vue-next';
import { registerDataset } from '@/api/dataset';
import { getDomainPresetCatalog, type DomainPreset } from '@/api/domainPreset';
import {
  datasetSubmissionErrorMessage,
  isAbsoluteDatasetDirectory,
} from '@/utils/datasetSubmission';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const router = useRouter();
const DEFAULT_STORAGE_DIRECTORY = '/Users/luchangfa/Documents/Codex/Data';

const name = ref('');
const description = ref('');
const storageDirectory = ref(DEFAULT_STORAGE_DIRECTORY);
const submitting = ref(false);
const domain = ref('general');
const presets = ref<DomainPreset[]>([]);
const presetError = ref(false);

async function loadPresets() {
  presetError.value = false;
  try { presets.value = (await getDomainPresetCatalog()).presets; }
  catch { presetError.value = true; }
}
onMounted(loadPresets);

async function submit() {
  if (submitting.value || !presets.value.length) return;

  const directory = storageDirectory.value.trim();
  if (!isAbsoluteDatasetDirectory(directory)) {
    showErrorToast('请填写不含 .. 路径段的本机绝对路径');
    return;
  }

  submitting.value = true;
  try {
    const result = await registerDataset({
      name: name.value.trim(),
      description: description.value.trim(),
      domain: domain.value,
      storage_directory: directory,
    });

    if (!result.dataset_id) throw new Error('数据集提交成功，但未返回数据集 ID');
    showSuccessToast('已长期保存到数据集列表，可随时再次分析');
    await router.push(`/dataset/seek/${encodeURIComponent(result.dataset_id)}`);
  } catch (error: unknown) {
    showErrorToast(datasetSubmissionErrorMessage(error));
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
.field-label {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  font-size: 0.8125rem;
  font-weight: 500;
}

.field-hint {
  font-size: 0.6875rem;
  font-weight: 400;
  color: var(--text-tertiary);
}

.required-mark {
  color: #dc5b52;
}

.form-field {
  width: 100%;
  border: 1px solid var(--border-main);
  border-radius: 0.5rem;
  background: var(--background-gray-main);
  padding: 0.625rem 0.75rem;
  color: var(--text-primary);
  outline: none;
  transition: border-color 150ms ease, box-shadow 150ms ease, background-color 150ms ease;
}

.form-field::placeholder {
  color: var(--text-tertiary);
}

.form-field:focus {
  border-color: #6b927f;
  background: var(--background-menu-white);
  box-shadow: 0 0 0 3px rgb(43 118 89 / 10%);
}

.submit-action {
  display: inline-flex;
  min-height: 2.5rem;
  align-items: center;
  justify-content: center;
  gap: 0.375rem;
  border-radius: 0.5rem;
  background: #226b51;
  padding: 0 1rem;
  color: white;
  font-size: 0.8125rem;
  font-weight: 500;
  transition: background-color 150ms ease, opacity 150ms ease;
}

.submit-action:hover:not(:disabled) {
  background: #19533e;
}

.submit-action:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
