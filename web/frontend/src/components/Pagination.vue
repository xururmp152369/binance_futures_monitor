<script setup lang="ts">
defineProps<{
  page: number
  perPage: number
  total: number
}>()

const emit = defineEmits<{
  (e: 'update:page', value: number): void
  (e: 'update:perPage', value: number): void
}>()

function pages(total: number, perPage: number) {
  return Math.max(1, Math.ceil(total / perPage))
}
</script>

<template>
  <div class="flex flex-wrap items-center justify-between gap-3 mt-4">
    <div class="flex items-center gap-2 text-sm text-gray-400">
      <span>每頁</span>
      <select
        :value="perPage"
        @change="emit('update:perPage', Number(($event.target as HTMLSelectElement).value))"
        class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-200 text-sm"
      >
        <option value="10">10</option>
        <option value="25">25</option>
        <option value="50">50</option>
      </select>
      <span>筆，共 {{ total }} 筆</span>
    </div>

    <div class="flex items-center gap-1">
      <button
        :disabled="page <= 1"
        @click="emit('update:page', page - 1)"
        class="px-3 py-1 rounded text-sm disabled:opacity-30 bg-gray-800 hover:bg-gray-700 border border-gray-700"
      >
        &lt;
      </button>
      <template v-for="p in pages(total, perPage)" :key="p">
        <button
          v-if="Math.abs(p - page) <= 2 || p === 1 || p === pages(total, perPage)"
          @click="emit('update:page', p)"
          :class="[
            'px-3 py-1 rounded text-sm border border-gray-700',
            p === page ? 'bg-indigo-600 text-white' : 'bg-gray-800 hover:bg-gray-700 text-gray-300'
          ]"
        >
          {{ p }}
        </button>
        <span
          v-else-if="p === page - 3 || p === page + 3"
          class="px-1 text-gray-500 text-sm"
        >…</span>
      </template>
      <button
        :disabled="page >= pages(total, perPage)"
        @click="emit('update:page', page + 1)"
        class="px-3 py-1 rounded text-sm disabled:opacity-30 bg-gray-800 hover:bg-gray-700 border border-gray-700"
      >
        &gt;
      </button>
    </div>
  </div>
</template>
