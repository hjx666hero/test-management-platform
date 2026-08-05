<template>
  <div>
    <div class="page-head">
      <h2>任务列表</h2>
      <el-button type="primary" @click="$router.push('/tasks/new')">+ 创建任务</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="tasks" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="任务名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="env_url" label="环境" min-width="180" show-overflow-tooltip />
        <el-table-column label="标签" width="200">
          <template #default="{ row }">
            <el-tag v-for="t in row.tags" :key="t" size="small" class="tag">{{ t }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="通过率" width="140">
          <template #default="{ row }">
            <span v-if="row.total === 0">-</span>
            <span v-else>{{ (row.passed / row.total * 100).toFixed(0) }}% ({{ row.passed }}/{{ row.total }})</span>
          </template>
        </el-table-column>
        <el-table-column prop="duration_s" label="耗时" width="90">
          <template #default="{ row }">{{ row.duration_s }}s</template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170" />
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="$router.push(`/tasks/${row.id}/report`)">报告</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { listTasks } from '../api'

const tasks = ref([])
const loading = ref(false)

const statusType = (s) => ({ running: 'warning', success: 'success', failed: 'danger', pending: 'info' }[s] || 'info')
const statusText = (s) => ({ running: '执行中', success: '通过', failed: '失败', pending: '排队中' }[s] || s)

onMounted(async () => {
  loading.value = true
  try { tasks.value = await listTasks() } finally { loading.value = false }
})
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; }
.tag { margin-right: 4px; }
</style>
