import { createRouter, createWebHistory } from 'vue-router'
import TaskList from '../views/TaskList.vue'
import CreateTask from '../views/CreateTask.vue'
import ReportDetail from '../views/ReportDetail.vue'

const routes = [
  { path: '/', redirect: '/tasks' },
  { path: '/tasks', component: TaskList },
  { path: '/tasks/new', component: CreateTask },
  { path: '/tasks/:id/report', component: ReportDetail, props: true },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
