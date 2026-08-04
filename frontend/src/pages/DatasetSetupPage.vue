<template>
  <div
    class="min-h-[100dvh] bg-[var(--background-gray-main)] px-4 py-8 text-[var(--text-primary)] sm:px-6 sm:py-12"
  >
    <main class="mx-auto w-full max-w-3xl">
      <header class="mb-7">
        <div
          class="mb-4 inline-flex items-center gap-2 rounded-full border border-[#b7d1c5] bg-[#edf6f1] px-3 py-1.5 text-xs font-medium text-[#226b51] dark:border-[#3f6453] dark:bg-[#21372d] dark:text-[#9bd0b8]"
        >
          <Database class="size-3.5" />
          科学数据探查 · 数据设置
        </div>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">准备待分析数据集</h1>
        <p class="mt-2 max-w-2xl text-sm leading-6 text-[var(--text-secondary)]">
          填写数据集描述和服务器存储目录。提交后将进入智能分析页面，目录仅用于本次测试分析，不会显示在地址栏或分析页面中。
        </p>
      </header>

      <form
        class="overflow-hidden rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-sm"
        autocomplete="off"
        @submit.prevent="submit"
      >
        <section class="space-y-5 p-5 sm:p-7">
          <div>
            <h2 class="text-sm font-semibold">基础信息</h2>
            <p class="mt-1 text-xs text-[var(--text-tertiary)]">用于在问答页面识别数据集并辅助 Agent 理解数据内容。</p>
          </div>

          <div class="grid gap-5 sm:grid-cols-2">
            <label class="field-label">
              <span>数据集 ID <span class="required-mark">*</span></span>
              <input
                v-model.trim="datasetExternalId"
                name="dataset_external_id"
                class="form-field"
                type="text"
                required
                maxlength="200"
                placeholder="例如：TPDC-QILIAN-001"
              />
              <span class="field-hint">填写业务系统中的数据集标识</span>
            </label>

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
          </div>

          <label class="field-label">
            <span>数据集摘要 <span class="required-mark">*</span></span>
            <textarea
              v-model.trim="summary"
              name="summary"
              class="form-field min-h-28 resize-y leading-6"
              required
              maxlength="4000"
              placeholder="简要说明数据内容、时空范围、数据格式和适用场景"
            />
          </label>

          <label class="field-label">
            <span>关键词 <span class="required-mark">*</span></span>
            <textarea
              v-model="keywordsText"
              name="keywords"
              class="form-field min-h-20 resize-y"
              required
              placeholder="降水, GeoTIFF, 祁连山（支持逗号或换行分隔）"
            />
            <span class="field-hint">提交时会自动移除空项和重复关键词</span>
          </label>
        </section>

        <section class="border-t border-[var(--border-main)] p-5 sm:p-7">
          <div>
            <div class="flex items-center gap-2 text-sm font-semibold">
              <Server class="size-4 text-[#2b7659]" />
              服务器存储目录 <span class="required-mark">*</span>
            </div>
            <p class="mt-1 text-xs leading-5 text-[var(--text-tertiary)]">
              填写包含本次待分析文件的服务器目录；分析时将以只读方式挂载整个目录。
            </p>
          </div>

          <div class="relative mt-4">
            <FileKey2
              class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--icon-secondary)]"
            />
            <input
              v-model="storageDirectory"
              name="storage_directory"
              class="form-field pl-9 font-mono text-xs"
              type="text"
              required
              spellcheck="false"
              aria-label="服务器存储目录"
              placeholder="/data/datasets/example"
            />
          </div>

          <div
            class="mt-4 flex items-start gap-2.5 rounded-lg border border-[#c6dbd1] bg-[#f3f8f5] px-3.5 py-3 text-xs leading-5 text-[#315f4c] dark:border-[#405f50] dark:bg-[#22332b] dark:text-[#a9cbbb]"
          >
            <ShieldCheck class="mt-0.5 size-4 shrink-0" />
            <span>
              这是临时测试请求：提交内容不写入数据集数据库，可使用相同信息重复提交。真实目录只通过本次 POST 请求发送，不会写入 URL、localStorage 或 sessionStorage。
            </span>
          </div>
        </section>

        <footer
          class="flex flex-col-reverse gap-3 border-t border-[var(--border-main)] bg-[var(--background-gray-main)] px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-7"
        >
          <p class="text-xs text-[var(--text-tertiary)]"><span class="required-mark">*</span> 为必填项</p>
          <button type="submit" class="submit-action" :disabled="submitting">
            <LoaderCircle v-if="submitting" class="size-4 animate-spin" />
            <ArrowRight v-else class="size-4" />
            {{ submitting ? '正在准备数据集…' : '提交并进入智能分析' }}
          </button>
        </footer>
      </form>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowRight, Database, FileKey2, LoaderCircle, Server, ShieldCheck } from 'lucide-vue-next';
import { createDatasetSubmission } from '@/api/dataset';
import { datasetSubmissionErrorMessage } from '@/utils/datasetSubmissionError';
import { showErrorToast } from '@/utils/toast';

const router = useRouter();

const datasetExternalId = ref('');
const name = ref('');
const summary = ref('');
const keywordsText = ref('');
const storageDirectory = ref('');
const submitting = ref(false);

function normalizedKeywords(): string[] {
  return [...new Set(keywordsText.value.split(/[,，\n\r]+/).map((keyword) => keyword.trim()).filter(Boolean))];
}

async function submit() {
  if (submitting.value) return;

  const directory = storageDirectory.value.trim();
  const keywords = normalizedKeywords();
  if (!keywords.length) {
    showErrorToast('请至少填写一个关键词');
    return;
  }
  if (!directory) {
    showErrorToast('请填写服务器存储目录');
    return;
  }

  submitting.value = true;
  try {
    const result = await createDatasetSubmission({
      external_id: datasetExternalId.value.trim(),
      name: name.value.trim(),
      summary: summary.value.trim(),
      keywords,
      storage_directory: directory,
    });

    if (!result.dataset_id) throw new Error('Dataset submission did not return an ID');
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
  align-items: center;
  justify-content: center;
  gap: 0.375rem;
  min-height: 2.5rem;
  border-radius: 0.5rem;
  background: #226b51;
  padding: 0 1rem;
  color: white;
  font-size: 0.8125rem;
  font-weight: 500;
  transition: background-color 150ms ease, border-color 150ms ease, opacity 150ms ease;
}

.submit-action:hover:not(:disabled) {
  background: #19533e;
}

.submit-action:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
