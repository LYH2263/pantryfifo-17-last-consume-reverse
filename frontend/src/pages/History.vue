<template>
  <div>
    <h1>消费履历</h1>
    <p class="muted">只能冲正最近一笔成功消费；冲正记录会标注在对应消费上。</p>
    <div v-for="h in rows" :key="h.id" class="hist-row">
      <template v-if="h.kind === 'reverse'">
        <span class="tag tag-rev">冲正</span>
        <strong>{{ h.item_name || '商品' }}</strong>
        <span class="muted">撤回消费 #{{ h.reverses_id }}</span>
        <span class="muted">原因：{{ h.note }}</span>
        <span class="muted">{{ fmt(h.created_at) }}</span>
      </template>

      <template v-else>
        <div class="hist-head">
          <span class="tag" :class="{ 'tag-done': h.reversed_at }">消费 #{{ h.id }}</span>
          <strong>{{ h.item_name || '商品' }}</strong>
          <span v-for="(d, i) in h.deductions" :key="i" class="lot">
            批#{{ d.lot_id }} ×{{ d.take }} · {{ d.expiry || '无到期' }}
          </span>
          <span class="muted">{{ fmt(h.created_at) }}</span>
          <span v-if="h.note" class="muted">备注：{{ h.note }}</span>
        </div>
        <div class="hist-ops">
          <span v-if="h.reversed_at" class="tag tag-done">已冲正（原因：{{ h.reverse_reason }}）</span>
          <button v-else-if="h.reversible" @click="openId = openId === h.id ? null : h.id">
            {{ openId === h.id ? '收起冲正' : '冲正此笔' }}
          </button>
          <button v-else disabled title="只能冲正最近一笔成功消费">仅可冲正最近一笔</button>
        </div>
        <ReversePanel v-if="h.reversible && openId === h.id" :target="h" @done="load" />
      </template>
    </div>
    <p v-if="!rows.length" class="muted">暂无消费记录。</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import ReversePanel from '../components/ReversePanel.vue'

const rows = ref([])
const openId = ref(null)

async function load() {
  rows.value = await api('/consumptions')
  if (!rows.value.some(h => h.id === openId.value && h.reversible)) openId.value = null
}

function fmt(iso) {
  if (!iso) return ''
  return iso.replace('T', ' ').slice(0, 16) + ' UTC'
}

onMounted(load)
</script>
