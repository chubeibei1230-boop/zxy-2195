import urllib.request
import urllib.parse
import json

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
    print("1. 测试根路径")
    code, data = req("GET", "/")
    print(f"  STATUS {code}: {data['app']} - {data['version']}")
    assert code == 200

    print("\n" + "=" * 60)
    print("2. 登录 - 管理员")
    code, data = req("POST", "/api/auth/login", {
        "username": "admin", "password": "admin123"
    })
    print(f"  STATUS {code}: {data.get('message', data.get('error', ''))}")
    assert code == 200, f"登录失败: {data}"
    admin_token = data["access_token"]
    print(f"  Token 获取成功, 用户: {data['user']['real_name']}/{data['user']['role_name']}")

    print("\n" + "=" * 60)
    print("3. 登录 - 阅卷员")
    code, data = req("POST", "/api/auth/login", {
        "username": "reviewer1", "password": "reviewer123"
    })
    assert code == 200
    reviewer_token = data["access_token"]
    print(f"  STATUS {code}: 阅卷员登录成功")

    print("\n" + "=" * 60)
    print("4. 登录 - 复核员")
    code, data = req("POST", "/api/auth/login", {
        "username": "auditor1", "password": "auditor123"
    })
    assert code == 200
    auditor_token = data["access_token"]
    print(f"  STATUS {code}: 复核员登录成功")

    print("\n" + "=" * 60)
    print("5. 管理员: 创建批次")
    code, data = req("POST", "/api/admin/batches", {
        "batch_code": "BATCH2024JUNE",
        "batch_name": "2024年6月语言测评批次",
        "description": "第二季度统一测评",
        "start_date": "2024-06-01",
        "end_date": "2024-06-30"
    }, admin_token)
    assert code == 201, f"创建批次失败: {data}"
    batch_id = data["id"]
    print(f"  STATUS {code}: 批次创建成功, ID={batch_id}")

    print("\n" + "=" * 60)
    print("6. 管理员: 创建题型组")
    code, data = req("POST", "/api/admin/question-groups", {
        "group_code": "QG_WRITING",
        "group_name": "写作题型组",
        "description": "包含作文、应用文写作",
        "batch_id": batch_id,
        "max_score": 100,
        "pass_score": 60
    }, admin_token)
    assert code == 201, f"创建题型组失败: {data}"
    qg_id = data["id"]
    print(f"  STATUS {code}: 题型组创建成功, ID={qg_id}")

    code, data = req("POST", "/api/admin/question-groups", {
        "group_code": "QG_READING",
        "group_name": "阅读题型组",
        "description": "阅读理解",
        "batch_id": batch_id,
        "max_score": 100,
        "pass_score": 60
    }, admin_token)
    assert code == 201
    qg2_id = data["id"]

    print("\n" + "=" * 60)
    print("7. 管理员: 创建评分规则")
    code, data = req("POST", "/api/admin/scoring-rules", {
        "rule_code": "RULE_WRITING_V1",
        "rule_name": "写作评分规则V1",
        "question_group_id": qg_id,
        "description": "评分标准: 内容40分+结构30分+语言30分",
        "score_guide": "90-100优秀, 80-89良好, 70-79中等, 60-69及格, 60以下不及格",
        "deduction_rules": "语法错误每处扣0.5, 字数不足扣5-10分"
    }, admin_token)
    assert code == 201, f"创建评分规则失败: {data}"
    print(f"  STATUS {code}: 评分规则创建成功")

    print("\n" + "=" * 60)
    print("8. 管理员: 创建责任组")
    code, data = req("POST", "/api/admin/responsibility-groups", {
        "group_code": "RG_WRITING_A",
        "group_name": "写作评阅A组",
        "description": "负责写作题型阅卷与复核",
        "batch_id": batch_id,
        "question_group_id": qg_id,
        "deadline_hours": 24
    }, admin_token)
    assert code == 201, f"创建责任组失败: {data}"
    rg_id = data["id"]
    print(f"  STATUS {code}: 责任组创建成功, ID={rg_id}")

    print("\n" + "=" * 60)
    print("9. 管理员: 批量创建试卷")
    papers = []
    for i in range(1, 8):
        papers.append({
            "paper_number": f"P{str(202406000 + i).zfill(8)}",
            "batch_id": batch_id,
            "question_group_id": qg_id if i <= 5 else qg2_id,
            "candidate_name": f"考生{i:02d}",
            "candidate_id": f"CAND{i:04d}"
        })
    code, data = req("POST", "/api/admin/papers/bulk", {"papers": papers}, admin_token)
    assert code == 201, f"批量创建试卷失败: {data}"
    print(f"  STATUS {code}: 批量创建成功: 创建{data['created']}份, 跳过{data['skipped']}份")

    print("\n" + "=" * 60)
    print("10. 管理员: 批量自动分配阅卷任务")
    code, data = req("POST", "/api/admin/tasks/batch-auto-assign", {
        "batch_id": batch_id,
        "task_type": "review",
        "reviewer_ids": [2, 3]
    }, admin_token)
    assert code == 200, f"分配失败: {data}"
    print(f"  STATUS {code}: {data['message']}, 分配{data['assigned']}/{data['total_papers']}份")

    print("\n" + "=" * 60)
    print("11. 阅卷员: 查看任务列表")
    code, data = req("GET", "/api/reviewer/tasks?only_mine=true", token=reviewer_token)
    assert code == 200, f"任务列表失败: {data}"
    task_items = data["items"]
    print(f"  STATUS {code}: 共{data['total']}个任务, 当前{len(task_items)}条")
    if task_items:
        sample = task_items[0]
        print(f"  示例: 任务ID={sample['id']}, 试卷={sample['paper_number']}, 状态={sample['status_name']}")

    print("\n" + "=" * 60)
    print("12. 阅卷员: 开始阅卷 + 提交初评 (试卷1-3分差, 4-5正常)")
    review_token = reviewer_token
    assigned = [t for t in task_items]
    submitted = 0
    for idx, t in enumerate(assigned):
        code, _ = req("POST", f"/api/reviewer/tasks/{t['id']}/start", token=review_token)
        if code != 200:
            continue
        score = 72 + idx * 5 if idx < 3 else 80 + idx
        diff_flag = idx == 1
        code, _ = req("POST", f"/api/reviewer/tasks/{t['id']}/submit", {
            "initial_score": score,
            "deduction_reason": f"扣分点{idx+1}: 论点不够清晰" if idx < 3 else "无明显扣分",
            "difficulty_flag": diff_flag,
            "difficulty_note": "此题答案有歧义，建议教研组讨论" if diff_flag else "",
            "completion_note": "阅卷完成，评分公正客观"
        }, token=review_token)
        if code == 200:
            submitted += 1
    print(f"  完成初评提交: {submitted}份")

    print("\n" + "=" * 60)
    print("13. 复核员: 查看待复核任务")
    code, data = req("GET", "/api/auditor/tasks", token=auditor_token)
    assert code == 200
    audit_items = data["items"]
    print(f"  STATUS {code}: 共{data['total']}个复核相关任务")

    print("\n" + "=" * 60)
    print("14. 管理员: 分配复核任务")
    to_audit_paper_ids = [3, 4, 5, 6]
    auditor_id = 4
    code, data = req("POST", "/api/admin/tasks/assign", {
        "paper_ids": to_audit_paper_ids,
        "assignee_id": auditor_id,
        "task_type": "audit"
    }, admin_token)
    print(f"  STATUS {code}: 分配复核: 成功{data['assigned']}, 跳过{len(data['skipped'])}")

    print("\n" + "=" * 60)
    print("15. 复核员: 领取任务 + 提交复核（制造分差）")
    code, tasks = req("GET", "/api/auditor/tasks", token=auditor_token)
    my_tasks = tasks["items"]
    audited = 0
    for idx, t in enumerate(my_tasks):
        if not t['assignee_id'] or t['assignee_id'] == auditor_id:
            code, _ = req("POST", f"/api/auditor/tasks/{t['id']}/accept", token=auditor_token)
            audit_score = 62 if idx == 0 else 83 if idx == 1 else 71
            diff_reason = "初评过高，内容跑题" if idx == 0 else "评分一致通过"
            code, sub_data = req("POST", f"/api/auditor/tasks/{t['id']}/submit", {
                "audit_score": audit_score,
                "diff_reason": diff_reason,
                "handling_opinion": "按复核分定分"
            }, token=auditor_token)
            if code == 200:
                audited += 1
                print(f"  试卷{t.get('paper_number')}: 分差{sub_data.get('score_diff', 'N/A')}, 状态={sub_data['status_name']}")
    print(f"  完成复核: {audited}份")

    print("\n" + "=" * 60)
    print("16. 查询API: 试卷多条件筛选")
    q = "/api/query/papers?batch_id=" + str(batch_id) + "&status=pending_audit"
    code, data = req("GET", q, token=admin_token)
    assert code == 200
    print(f"  STATUS {code}: 待复核试卷={data['total']}份")

    print("\n" + "=" * 60)
    print("17. 查询API: 分差排行")
    code, data = req("GET", "/api/query/score-diff-ranking?batch_id=" + str(batch_id), token=admin_token)
    assert code == 200
    print(f"  STATUS {code}: 共{len(data['ranking'])}份有分差, 平均分差={data['summary'].get('avg_diff')}")
    if data['ranking']:
        print(f"  TOP1: {data['ranking'][0]['paper_number']} 分差={data['ranking'][0]['score_diff']}")

    print("\n" + "=" * 60)
    print("18. 查询API: 待复核列表")
    code, data = req("GET", "/api/query/pending-audit-list?batch_id=" + str(batch_id), token=auditor_token)
    assert code == 200
    print(f"  STATUS {code}: 待处理={data['total']}, 超时={data['urgency_summary'].get('timeout_count')}, 紧急={data['urgency_summary'].get('urgent_count')}")

    print("\n" + "=" * 60)
    print("19. 查询API: 题型疑难趋势")
    code, data = req("GET", "/api/query/difficulty-trend?batch_id=" + str(batch_id) + "&days=7", token=admin_token)
    assert code == 200
    print(f"  STATUS {code}: 共{len(data['by_question_group'])}个题型组有疑难标记")

    print("\n" + "=" * 60)
    print("20. 管理员: 触发异常检测 + 查看告警")
    code, data = req("POST", "/api/alerts/detect-anomalies", token=admin_token)
    assert code == 200, f"检测失败: {data}"
    print(f"  STATUS {code}: 新增告警: {data['new_alerts_count']}条")
    code, data = req("GET", "/api/alerts?is_handled=false", token=admin_token)
    assert code == 200
    print(f"  未处理告警: {data['statistics'].get('unhandled', 0)}条")
    print(f"    分差过大: {data['statistics'].get('score_diff_count', 0)}")
    print(f"    阅卷超时: {data['statistics'].get('timeout_count', 0)}")
    print(f"    疑难集中: {data['statistics'].get('difficulty_count', 0)}")
    print(f"    复核未定分: {data['statistics'].get('unfinalized_count', 0)}")
    print(f"    责任组积压: {data['statistics'].get('backlog_count', 0)}")

    print("\n" + "=" * 60)
    print("21. 统计摘要")
    code, data = req("GET", "/api/query/statistics/summary?batch_id=" + str(batch_id), token=admin_token)
    assert code == 200
    dist = data['paper_status_distribution']
    print(f"  STATUS {code}:")
    print(f"    试卷总数: {dist['total']}")
    print(f"    阅卷中: {dist['reviewing']}  待复核: {dist['pending_audit']}  已定分: {dist['finalized']}  差异待处理: {dist['diff_pending']}")
    s = data['scoring_statistics']
    if s and s.get('scored_count'):
        print(f"    平均分(初评): {round(s['avg_initial_score'], 1) if s['avg_initial_score'] else 'N/A'}")
        print(f"    平均分(最终): {round(s['avg_final_score'], 1) if s['avg_final_score'] else 'N/A'}")

    print("\n" + "=" * 60)
    print("✅ 所有测试通过! 系统运行正常!")
    print("=" * 60)


if __name__ == "__main__":
    test()
