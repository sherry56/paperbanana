# 权限管理说明（生成/计费/任务）

## 角色模型

系统内两类角色：
- `admin`：管理员账号。拥有全部档位与无限制生成额度（`gen_quota_remaining = None`），可直接运行生成。
- `user`：普通用户。是否能生成由 `authorized`（授权状态）和 `gen_quota_remaining`（剩余额度）共同决定。

## 生成是否允许（关键门禁）

HTTP `POST /generate` 在服务端会做两步校验：
1. **授权校验**
   - 非管理员（`role != admin`）且 `authorized == false`：直接拒绝生成，并提示“请先在账号与申请提交申请并等待管理员审核”。
2. **额度校验（计费）**
   - `consume_user_generation_quota` 根据本次预计成本 `need` 校验 `gen_quota_remaining` 是否足够：
     - `gen_quota_remaining == None`：视为无限额度，不扣减。
     - `gen_quota_remaining < need`：拒绝生成，不会产生任务。
     - 通过校验后，先扣减额度，再异步创建生成任务。

> 说明：轮询接口（`GET /generate/job/{job_id}`）只读取任务状态，不会触发新的模型调用，因此不会产生额外 token 消耗。

## 任务去重（避免重复扣减/重复调用）

为避免浏览器重试、网络抖动或双击导致重复 `POST /generate`：
- `POST /generate` 会读取会话 `pending_generation_job_id`
- 若会话里已有任务，且该任务在服务端状态为：
  - `running`：重定向到 `/generate/wait/{job_id}`，不再新建任务。
  - `done` 或 `error`：重定向到 `/generate/finish/{job_id}`，由后端领取结果/展示错误。
- 若 `pending_generation_job_id` 已不存在对应任务，则清理该会话键，允许用户重新发起生成。

## 典型流程

1. 用户注册：`authorized=false`，`gen_quota_remaining=0`。
2. 用户申请：管理员审核后将用户置为 `authorized=true`（档位/编辑权限随申请类型变化）。
3. 管理员手动授权次数：通过后台配置 `gen_quota_remaining`，普通用户开始具备“可生成”的额度条件。
4. 用户提交生成：服务端先授权校验，再扣减额度并创建生成任务；生成结果异步落盘。

## 需要关注的管理后台操作

管理员应重点检查：
- 对普通用户是否保持 `authorized=true` 与 `gen_quota_remaining` 一致。
- 对于已经授权但额度为 0 的用户，生成会被额度校验拒绝；此时应通过后台“授权次数/配额”修正。

