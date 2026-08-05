<template>
  <el-card shadow="never" class="form-card">
    <h2>创建测试任务</h2>
    <el-form :model="form" label-width="100px" style="max-width: 560px">
      <el-form-item label="任务名" required>
        <el-input v-model="form.name" placeholder="如: 全量回归-20260805" />
      </el-form-item>
      <el-form-item label="环境">
        <el-select v-model="form.env_url" style="width: 100%">
          <el-option label="本地环境 (localhost:8080)" value="http://localhost:8080/api" />
        </el-select>
      </el-form-item>
      <el-form-item label="用例标签">
        <el-checkbox-group v-model="form.tags">
          <el-checkbox label="P0">P0 核心</el-checkbox>
          <el-checkbox label="P1">P1 重要</el-checkbox>
          <el-checkbox label="P2">P2 一般</el-checkbox>
          <el-checkbox label="articles">文章</el-checkbox>
          <el-checkbox label="user">用户</el-checkbox>
          <el-checkbox label="tags">标签</el-checkbox>
          <el-checkbox label="profiles">关注</el-checkbox>
          <el-checkbox label="comments">评论</el-checkbox>
          <el-checkbox label="favorites">收藏</el-checkbox>
        </el-checkbox-group>
        <div class="tip">不选 = 执行全部用例</div>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="submitting" @click="submit">创建并执行</el-button>
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { createTask } from '../api'

const router = useRouter()
const form = reactive({ name: '', env_url: 'http://localhost:8080/api', tags: [] })
const submitting = ref(false)

async function submit() {
  if (!form.name.trim()) return ElMessage.warning('请填写任务名')
  submitting.value = true
  try {
    const task = await createTask({ ...form })
    ElMessage.success(`任务 #${task.id} 已创建,开始异步执行`)
    router.push(`/tasks/${task.id}/report`)
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.form-card { max-width: 720px; }
.tip { font-size: 12px; color: #909399; margin-top: 6px; }
</style>
