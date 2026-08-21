<template>
  <el-container class="layout">
    <el-header class="header">
      <div class="logo">🛠 测试管理平台</div>
      <el-menu mode="horizontal" :default-active="$route.path" router class="menu">
        <el-menu-item index="/tasks">任务列表</el-menu-item>
        <el-menu-item index="/tasks/new">创建任务</el-menu-item>
        <el-menu-item index="/fixes">修复建议</el-menu-item>
      </el-menu>
    </el-header>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
</script>

<style>
body { margin: 0; font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: #f5f7fa; }
.layout { min-height: 100vh; }
.header { display: flex; align-items: center; gap: 32px; background: #fff; border-bottom: 1px solid #e4e7ed; }
.logo { font-size: 18px; font-weight: 600; color: #409eff; white-space: nowrap; }
.menu { flex: 1; border-bottom: none !important; }
.el-main { max-width: 1200px; width: 100%; margin: 0 auto; }
</style>
