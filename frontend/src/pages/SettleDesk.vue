<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const accounts = ref([])
const contributions = ref([])
const recalcRuns = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

const recalcAccount = ref('')
const recalcDay = ref('2026-11-16')
const recalcBusy = ref(false)
const recalcErr = ref('')
const report = ref(null)

async function load() {
  const [r, a, c, rr] = await Promise.all([
    getJson('/api/settle-runs'),
    getJson('/api/accounts'),
    getJson('/api/day-contributions'),
    getJson('/api/day-recalc-runs'),
  ])
  runs.value = r
  accounts.value = a
  contributions.value = c
  recalcRuns.value = rr
  if (!recalcAccount.value && a.length) recalcAccount.value = String(a[0].id)
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：净额 ${(r.net_cents / 100).toFixed(2)}，隔离 ${r.quarantine_count}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function runRecalc() {
  if (!recalcAccount.value || !recalcDay.value) {
    recalcErr.value = '请选择账户并填写入账日'
    return
  }
  recalcBusy.value = true
  recalcErr.value = ''
  try {
    const r = await postJson('/api/day-recalc-runs', {
      account_id: Number(recalcAccount.value),
      day: recalcDay.value,
    })
    report.value = r.report || null
    await load()
  } catch (e) {
    report.value = null
    recalcErr.value = String(e.message || e)
    await load()
  } finally {
    recalcBusy.value = false
  }
}

async function showRun(runId) {
  recalcErr.value = ''
  try {
    const r = await getJson(`/api/day-recalc-runs/${runId}`)
    if (r.status === '完成' && r.report && r.report.入账日) {
      report.value = r.report
    } else {
      report.value = null
      recalcErr.value = `重算运行 #${runId}（${r.status}）${r.error ? '：' + r.error : '：无守恒报告'}`
    }
  } catch (e) {
    recalcErr.value = String(e.message || e)
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function provenance(c) {
  if (c.recalc_run_id) return `重算#${c.recalc_run_id}`
  if (c.settle_run_id) return `归并#${c.settle_run_id}`
  return '—'
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：配平落余额，无原单/超额进隔离；并可按入账日重算当日净贡献。</p>
    <p class="note-strip">本轮只处理状态为「待归并」的流水；隔离项不计入账户余额。归并后各入账日净贡献物化于日贡献表，账户余额 = Σ 日贡献。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th><th>净额（元）</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="8">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>按日重算（日切片净额）</h2>
      <p class="note-strip">
        指定账户 + 入账日，从已归并流水重算「日净贡献 = 当日正向合计 − 当日已配平冲正合计」并整格写回该日贡献；
        邻日贡献保持不变。守恒校验不过则整次回滚，日贡献与总余额回到重算前。
      </p>
      <div class="row-actions">
        <select v-model="recalcAccount" class="sel">
          <option v-for="a in accounts" :key="a.id" :value="String(a.id)">
            {{ a.code }} · {{ a.name }}
          </option>
        </select>
        <input v-model="recalcDay" type="date" class="sel" />
        <button class="btn" :disabled="recalcBusy" @click="runRecalc">重算该日净贡献</button>
      </div>
      <p v-if="recalcErr" class="msg">{{ recalcErr }}</p>

      <div v-if="report && report.入账日" class="report">
        <div class="stats">
          <div class="stat">
            <div class="k">日正向（元）</div>
            <div class="v">{{ yuan(report.日正向分) }}</div>
          </div>
          <div class="stat">
            <div class="k">日冲正（元）</div>
            <div class="v">{{ yuan(report.日冲正分) }}</div>
          </div>
          <div class="stat">
            <div class="k">日净贡献（元）</div>
            <div class="v">{{ yuan(report.日净贡献分) }}</div>
          </div>
        </div>
        <table>
          <tbody>
            <tr><th>账户</th><td>{{ report.账户 }} · {{ report.账户名 }}</td></tr>
            <tr><th>入账日</th><td>{{ report.入账日 }}</td></tr>
            <tr><th>重算前日净（元）</th><td>{{ yuan(report.重算前日净分) }}</td></tr>
            <tr>
              <th>账户余额（元）</th>
              <td>{{ yuan(report.余额前分) }} → {{ yuan(report.余额后分) }}</td>
            </tr>
            <tr><th>该日待归并笔数</th><td>{{ report.该日待归并笔数 }}</td></tr>
            <tr>
              <th>同源校验</th>
              <td>
                日贡献合计 {{ yuan(report.同源校验.日贡献合计分) }}
                = 流水净额 {{ yuan(report.同源校验.流水净额分) }}
                = 账户余额 {{ yuan(report.同源校验.账户余额分) }}
                <span class="tag" :class="report.同源校验.守恒 ? 'on' : 'bad'">
                  {{ report.同源校验.守恒 ? '守恒' : '不守恒' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <h3 class="sub">邻日抽检（必须不变）</h3>
        <table>
          <thead>
            <tr><th>入账日</th><th>重算前（元）</th><th>重算后（元）</th><th>不变</th></tr>
          </thead>
          <tbody>
            <tr v-for="n in report.邻日抽检" :key="n.入账日">
              <td>{{ n.入账日 }}</td>
              <td>{{ yuan(n.前) }}</td>
              <td>{{ yuan(n.后) }}</td>
              <td><span class="tag" :class="n.不变 ? 'on' : 'bad'">{{ n.不变 ? '是' : '否' }}</span></td>
            </tr>
            <tr v-if="!report.邻日抽检.length"><td colspan="4">该账户无其他入账日贡献</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <h2>日贡献（物化分日净额）</h2>
      <table>
        <thead>
          <tr>
            <th>账户</th><th>入账日</th><th>正向（元）</th><th>冲正（元）</th><th>净贡献（元）</th><th>来源</th><th>更新</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in contributions" :key="c.account_id + '-' + c.day">
            <td>{{ c.account_code }}</td>
            <td>{{ c.day }}</td>
            <td>{{ yuan(c.forward_cents) }}</td>
            <td>{{ yuan(c.reversal_cents) }}</td>
            <td>{{ yuan(c.net_cents) }}</td>
            <td><span class="tag">{{ provenance(c) }}</span></td>
            <td>{{ c.updated_at }}</td>
          </tr>
          <tr v-if="!contributions.length"><td colspan="7">尚无日贡献——先跑净额归并</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>重算运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>账户</th><th>入账日</th><th>状态</th><th>日净（元）</th><th>余额前→后（元）</th><th>时间</th><th>报告</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in recalcRuns" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.account_code }}</td>
            <td>{{ r.day }}</td>
            <td>
              <span class="tag" :class="r.status === '完成' ? 'on' : (r.status === '失败' ? 'bad' : 'warn')">
                {{ r.status }}
              </span>
            </td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ yuan(r.balance_before_cents) }} → {{ yuan(r.balance_after_cents) }}</td>
            <td>{{ r.created_at }}</td>
            <td><button class="btn secondary" @click="showRun(r.id)">查看</button></td>
          </tr>
          <tr v-if="!recalcRuns.length"><td colspan="8">尚无重算运行</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.sel {
  font: inherit;
  font-size: 13px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--ink);
  border-radius: 2px;
}
.report { margin-top: 6px; }
.report .stats { margin-bottom: 12px; }
.sub {
  margin: 16px 0 8px;
  font-family: var(--display);
  font-size: 15px;
  letter-spacing: 0.08em;
}
</style>
