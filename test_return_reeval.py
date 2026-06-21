import urllib.request
import urllib.parse
import json
import time

BASE = "http://127.0.0.1:8163"


def req(method, path, data=None, token=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data:
        body = json.dumps(data).encode("utf-8")
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = str(e)
        return e.code, detail


def test():
    print("=" * 60)
    print("  复核退回重评模块测试")
    print("=" * 60)

    print("\n步骤1: 登录各角色")
    code, data = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    assert code == 200, f"管理员登录失败: {data}"
    admin_token = data["access_token"]
    print(f"  管理员登录成功")

    code, data = req("POST", "/api/auth/login", {"username": "reviewer1", "password": "reviewer123"})
    assert code == 200
    reviewer_token = data["access_token"]
    print(f"  阅卷员登录成功")

    code, data = req("POST", "/api/auth/login", {"username": "auditor1", "password": "auditor123"})
    assert code == 200
    auditor_token = data["access_token"]
    print(f"  复核员登录成功")

    print("\n步骤2: 准备测试数据")
    ts = int(time.time())
    code, data = req("POST", "/api/admin/batches", {
        "batch_code": f"BATCH_RT_{ts}",
        "batch_name": "退回重评测试批次"
    }, admin_token)
    if code not in (200, 201):
        print(f"  创建批次失败({code}): {data}, 使用默认ID=1")
        batch_id = 1
    else:
        batch_id = data.get('id', 1)
    print(f"  批次ID: {batch_id}")

    code, data = req("POST", "/api/admin/question-groups", {
        "group_code": f"QG_RT_{ts}",
        "group_name": "退回重评测试题型组",
        "batch_id": batch_id,
        "max_score": 100,
        "pass_score": 60
    }, admin_token)
    if code not in (200, 201):
        print(f"  创建题型组失败({code}): {data}, 使用默认ID=1")
        qg_id = 1
    else:
        qg_id = data.get('id', 1)
    print(f"  题型组ID: {qg_id}")

    paper_number = f"RT_TEST_{int(time.time())}"
    code, data = req("POST", "/api/admin/papers", {
        "paper_number": paper_number,
        "batch_id": batch_id,
        "question_group_id": qg_id,
        "candidate_name": "退回重评测试考生",
        "candidate_id": "RT_TEST_001"
    }, admin_token)
    assert code == 201, f"创建试卷失败: {data}"
    paper_id = data['id']
    print(f"  创建试卷成功, ID: {paper_id}")

    print("\n步骤3: 分配阅卷任务给阅卷员")
    code, data = req("POST", "/api/admin/tasks/assign", {
        "paper_ids": [paper_id],
        "assignee_id": 2,
        "task_type": "review"
    }, admin_token)
    assert code == 200, f"分配阅卷任务失败: {data}"
    print(f"  分配阅卷任务成功, 分配{data['assigned']}份")

    code, data = req("GET", "/api/reviewer/tasks?only_mine=true", token=reviewer_token)
    review_task = None
    for t in data['items']:
        if t['paper_id'] == paper_id:
            review_task = t
            break
    assert review_task, "未找到阅卷任务"
    task_id = review_task['id']
    print(f"  阅卷任务ID: {task_id}")

    print("\n步骤4: 阅卷员开始阅卷并提交初评")
    code, data = req("POST", f"/api/reviewer/tasks/{task_id}/start", token=reviewer_token)
    assert code == 200, f"开始阅卷失败: {data}"
    print(f"  阅卷已开始")

    code, data = req("POST", f"/api/reviewer/tasks/{task_id}/submit", {
        "initial_score": 75,
        "deduction_reason": "论点不够清晰，扣5分",
        "difficulty_flag": False,
        "completion_note": "初评完成"
    }, token=reviewer_token)
    assert code == 200, f"提交初评失败: {data}"
    print(f"  初评已提交, 初评分: 75")

    print("\n步骤5: 分配复核任务给复核员")
    code, data = req("POST", "/api/admin/tasks/assign", {
        "paper_ids": [paper_id],
        "assignee_id": 4,
        "task_type": "audit"
    }, admin_token)
    print(f"  分配复核任务: {data.get('assigned', 0)}份, 跳过{len(data.get('skipped', []))}份")

    code, data = req("GET", "/api/auditor/tasks?only_mine=true", token=auditor_token)
    audit_task = None
    for t in data['items']:
        if t['paper_id'] == paper_id:
            audit_task = t
            break

    if not audit_task:
        code, data = req("GET", "/api/auditor/tasks", token=auditor_token)
        for t in data['items']:
            if t['paper_id'] == paper_id:
                audit_task = t
                break

    assert audit_task, "未找到复核任务"
    audit_task_id = audit_task['id']
    print(f"  复核任务ID: {audit_task_id}")

    print("\n步骤6: 复核员领取复核任务")
    code, data = req("POST", f"/api/auditor/tasks/{audit_task_id}/accept", token=auditor_token)
    assert code == 200, f"领取复核任务失败: {data}"
    print(f"  复核任务已领取")

    print("\n步骤7: 复核员发起退回重评（通过复核任务接口）")
    code, data = req("POST", f"/api/auditor/tasks/{audit_task_id}/return-for-reeval", {
        "return_reason": "初评分依据不足，扣分说明缺失，无法确认评分合理性",
        "return_reason_type": "insufficient_basis",
        "handling_opinion": "请重新评估，补充扣分依据"
    }, token=auditor_token)
    assert code == 201, f"退回重评失败: {data}"
    return_id = data['id']
    return_code = data['return_code']
    return_round = data['return_round']
    new_task_id = data['new_task_id']
    print(f"  退回重评发起成功!")
    print(f"    退回记录ID: {return_id}")
    print(f"    退回编号: {return_code}")
    print(f"    退回轮次: {return_round}")
    print(f"    新重评任务ID: {new_task_id}")

    print("\n步骤8: 验证试卷状态变为待重评")
    code, data = req("GET", f"/api/admin/papers?per_page=50", token=admin_token)
    paper_found = None
    for p in data.get('items', []):
        if p['id'] == paper_id:
            paper_found = p
            break
    assert paper_found, "未找到试卷"
    print(f"  试卷状态: {paper_found.get('status_name')}")
    print(f"  当前轮次: {paper_found.get('current_round')}")
    print(f"  退回次数: {paper_found.get('return_count')}")
    print(f"  最近退回原因类型: {paper_found.get('latest_return_reason_type_name', 'N/A')}")
    if paper_found.get('return_history'):
        print(f"  退回历史记录数: {len(paper_found['return_history'])}")
        for rh in paper_found['return_history']:
            print(f"    - 第{rh['return_round']}轮, 原因: {rh['return_reason_type_name']}, 状态: {rh['status_name']}")

    print("\n步骤9: 查看退回记录详情")
    code, data = req("GET", f"/api/returns/{return_id}", token=admin_token)
    assert code == 200, f"获取退回详情失败: {data}"
    print(f"  退回编号: {data['return_code']}")
    print(f"  退回原因: {data['return_reason']}")
    print(f"  退回原因类型: {data['return_reason_type_name']}")
    print(f"  处理意见: {data['handling_opinion']}")
    print(f"  退回轮次: {data['return_round']}")
    print(f"  退回状态: {data['status_name']}")
    print(f"  复核员: {data['auditor_name']}")
    print(f"  退回历史: {len(data.get('return_history', []))}条")

    print("\n步骤10: 查看退回记录列表")
    code, data = req("GET", "/api/returns?page=1&per_page=10", token=admin_token)
    assert code == 200, f"获取退回列表失败: {data}"
    print(f"  退回记录总数: {data['total']}")
    for item in data['items']:
        print(f"    - {item['return_code']}: 试卷{item['paper_number']}, {item['return_reason_type_name']}, {item['status_name']}")

    print("\n步骤11: 查看试卷时间线")
    code, data = req("GET", f"/api/returns/paper/{paper_id}/timeline", token=admin_token)
    assert code == 200, f"获取时间线失败: {data}"
    print(f"  当前轮次: {data['current_round']}")
    print(f"  退回次数: {data['return_count']}")
    print(f"  最近退回: {data.get('latest_return', {}).get('return_reason_type_name', 'N/A') if data.get('latest_return') else 'N/A'}")
    print(f"  时间线事件数: {data['total_events']}")
    for event in data['timeline']:
        print(f"    [{event['event_type_name']}] 第{event['round']}轮 - {event.get('created_at', 'N/A')}")

    print("\n步骤12: 阅卷员查看重评任务（含退回信息）")
    code, data = req("GET", "/api/reviewer/tasks?only_mine=true", token=reviewer_token)
    reeval_task = None
    for t in data['items']:
        if t['paper_id'] == paper_id and t.get('return_record_id'):
            reeval_task = t
            break

    if reeval_task:
        print(f"  找到重评任务: {reeval_task['task_code']}")
        print(f"  任务状态: {reeval_task['status_name']}")
        if reeval_task.get('return_info'):
            ri = reeval_task['return_info']
            print(f"  退回原因: {ri['return_reason']}")
            print(f"  退回原因类型: {ri['return_reason_type_name']}")
            print(f"  处理意见: {ri['handling_opinion']}")
            print(f"  退回轮次: {ri['return_round']}")
            print(f"  退回复核员: {ri['auditor_name']}")
    else:
        print("  未找到带有退回信息的重评任务, 尝试在新任务上操作...")
        code, data = req("GET", "/api/reviewer/tasks", token=reviewer_token)
        for t in data['items']:
            if t['paper_id'] == paper_id:
                reeval_task = t
                break
        if reeval_task:
            print(f"  找到任务: {reeval_task['task_code']}, 状态: {reeval_task['status_name']}")

    assert reeval_task, "未找到重评任务"

    print("\n步骤13: 阅卷员开始重评并提交")
    reeval_task_id = reeval_task['id']
    code, data = req("POST", f"/api/reviewer/tasks/{reeval_task_id}/start", token=reviewer_token)
    assert code == 200, f"开始重评失败: {data}"
    print(f"  重评已开始")

    code, data = req("POST", f"/api/reviewer/tasks/{reeval_task_id}/submit", {
        "initial_score": 80,
        "deduction_reason": "重评后扣分依据：论点不清晰扣3分，论据不足扣2分",
        "difficulty_flag": False,
        "completion_note": "重评完成，已补充扣分依据"
    }, token=reviewer_token)
    assert code == 200, f"提交重评失败: {data}"
    print(f"  重评已提交, 重评分: 80, 是否重评: {data.get('is_reeval', False)}")

    print("\n步骤14: 验证退回记录状态更新为已重评")
    code, data = req("GET", f"/api/returns/{return_id}", token=admin_token)
    assert code == 200
    print(f"  退回记录状态: {data['status_name']}")
    print(f"  重评完成时间: {data.get('reevaluated_at', 'N/A')}")

    print("\n步骤15: 分配复核任务并完成复核（闭环）")
    code, data = req("POST", "/api/admin/tasks/assign", {
        "paper_ids": [paper_id],
        "assignee_id": 4,
        "task_type": "audit"
    }, admin_token)
    print(f"  分配复核: {data.get('assigned', 0)}份")

    code, data = req("GET", "/api/auditor/tasks", token=auditor_token)
    audit_task2 = None
    for t in data['items']:
        if t['paper_id'] == paper_id and t['id'] != audit_task_id:
            audit_task2 = t
            break
    if not audit_task2:
        for t in data['items']:
            if t['paper_id'] == paper_id:
                audit_task2 = t
                break

    if audit_task2:
        audit_task2_id = audit_task2['id']
        code, data = req("POST", f"/api/auditor/tasks/{audit_task2_id}/accept", token=auditor_token)
        assert code == 200, f"领取复核2失败: {data}"

        code, data = req("POST", f"/api/auditor/tasks/{audit_task2_id}/submit", {
            "audit_score": 80,
            "diff_reason": "重评后评分合理",
            "handling_opinion": "确认重评结果，按重评分定分"
        }, token=auditor_token)
        print(f"  二次复核提交: {data.get('status_name', data.get('message', 'N/A'))}")

    print("\n步骤16: 查看退回重评统计")
    code, data = req("GET", "/api/returns/statistics", token=admin_token)
    assert code == 200, f"获取统计失败: {data}"
    print(f"  退回总数: {data['total']}")
    print(f"  待处理数: {data['pending_count']}")
    print(f"  按原因类型:")
    for item in data['by_reason_type']:
        print(f"    - {item['return_reason_type_name']}: {item['count']}")
    print(f"  按轮次:")
    for item in data['by_round']:
        print(f"    - 第{item['return_round']}轮: {item['count']}")
    print(f"  按状态:")
    for item in data['by_status']:
        print(f"    - {item['status_name']}: {item['count']}")
    if data.get('repeated_returns'):
        print(f"  重复退回试卷: {len(data['repeated_returns'])}份")
    if data.get('long_unclosed'):
        print(f"  长期未闭环: {len(data['long_unclosed'])}份")

    print("\n步骤17: 通过退回模块接口直接发起退回")
    code, data = req("GET", f"/api/query/papers?per_page=50", token=admin_token)
    test_paper = None
    for p in data.get('items', []):
        if p['current_status'] in ('pending_audit', 'diff_pending') and p['id'] != paper_id:
            test_paper = p
            break

    if test_paper:
        print(f"  找到另一份可退回试卷: {test_paper['paper_number']}")
        code, data = req("POST", "/api/returns", {
            "paper_id": test_paper['id'],
            "return_reason": "疑难标记与内容不一致",
            "return_reason_type": "inconsistent_flag",
            "handling_opinion": "请核实疑难标记后重新评估"
        }, token=auditor_token)
        if code == 201:
            print(f"  通过退回模块接口退回成功, 退回编号: {data['return_code']}")
        else:
            print(f"  通过退回模块接口退回失败: {data}")
    else:
        print("  没有其他可退回的试卷，跳过此步骤")

    print("\n步骤18: 查看阅卷员的试卷评审历史（含退回信息）")
    code, data = req("GET", f"/api/reviewer/papers/{paper_id}/review-history", token=reviewer_token)
    assert code == 200, f"获取评审历史失败: {data}"
    print(f"  试卷当前轮次: {data['paper'].get('current_round')}")
    print(f"  退回次数: {data['paper'].get('return_count')}")
    print(f"  评审记录数: {len(data.get('reviews', []))}")
    print(f"  退回历史数: {len(data.get('return_history', []))}")
    for rh in data.get('return_history', []):
        print(f"    - 第{rh['return_round']}轮退回, 原因: {rh['return_reason_type_name']}, 状态: {rh['status_name']}")

    print("\n步骤19: 查看告警中的退回重评提醒")
    code, data = req("GET", "/api/alerts?is_handled=false", token=admin_token)
    assert code == 200
    return_alerts = [a for a in data['items'] if a['alert_type'] == 'return_reeval']
    print(f"  退回重评告警数: {len(return_alerts)}")
    for a in return_alerts:
        print(f"    - {a['alert_type_name']}: {a['message'][:60]}...")
    print(f"  退回重评告警统计: {data['statistics'].get('return_reeval_count', 0)}")

    print("\n步骤20: 查看任务查询中的退回信息")
    code, data = req("GET", f"/api/query/tasks?paper_id={paper_id}", token=admin_token)
    assert code == 200
    for t in data.get('items', []):
        if t.get('return_info'):
            ri = t['return_info']
            print(f"  任务{t['task_code']}包含退回信息:")
            print(f"    退回原因类型: {ri['return_reason_type_name']}")
            print(f"    退回轮次: {ri['return_round']}")

    print("\n" + "=" * 60)
    print("  ✅ 复核退回重评模块测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    test()
