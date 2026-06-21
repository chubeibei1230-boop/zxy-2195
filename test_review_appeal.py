import requests
import json

base_url = 'http://127.0.0.1:8163'

def login(username, password):
    resp = requests.post(f'{base_url}/api/auth/login', json={
        'username': username,
        'password': password
    })
    return resp.json().get('access_token')

def get_headers(token):
    return {'Authorization': f'Bearer {token}'}

def print_result(title, resp):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"  STATUS: {resp.status_code}")
    try:
        data = resp.json()
        print(f"  RESULT: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
    except:
        print(f"  RESULT: {resp.text[:500]}")
    print(f"{'='*60}")

def test():
    print("\n" + "="*60)
    print("  试卷复评申请模块测试")
    print("="*60)

    admin_token = login('admin', 'admin123')
    print(f"\n管理员登录成功")
    admin_headers = get_headers(admin_token)

    print(f"\n{'='*60}")
    print(f"  步骤1: 准备测试数据（批次、题型组、试卷）")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/admin/batches', headers=admin_headers)
    batches = resp.json()
    if not batches:
        print("没有批次数据，创建一个...")
        resp = requests.post(f'{base_url}/api/admin/batches', json={
            'batch_code': 'BATCH_TEST_001',
            'batch_name': '测试批次001',
            'description': '用于复评测试的批次'
        }, headers=admin_headers)
        batch_id = resp.json().get('id')
        print(f"创建批次成功, ID: {batch_id}")
    else:
        batch_id = batches[0]['id']
        print(f"使用已有批次: {batches[0]['batch_name']}, ID: {batch_id}")
    
    resp = requests.get(f'{base_url}/api/admin/question-groups', headers=admin_headers)
    qgs = resp.json()
    if not qgs:
        print("没有题型组数据，创建一个...")
        resp = requests.post(f'{base_url}/api/admin/question-groups', json={
            'group_code': 'QG_TEST_001',
            'group_name': '测试题型组001',
            'max_score': 100,
            'pass_score': 60,
            'description': '用于复评测试的题型组'
        }, headers=admin_headers)
        qg_id = resp.json().get('id')
        print(f"创建题型组成功, ID: {qg_id}")
    else:
        qg_id = qgs[0]['id']
        print(f"使用已有题型组: {qgs[0]['group_name']}, ID: {qg_id}")
    
    resp = requests.get(f'{base_url}/api/admin/papers?page=1&per_page=5', headers=admin_headers)
    papers = resp.json().get('items', [])
    
    paper_id = None
    for p in papers:
        if p['current_status'] in ('pending_audit', 'diff_pending', 'finalized'):
            paper_id = p['id']
            print(f"找到符合条件的试卷: {p['paper_number']}, 状态: {p['current_status']}")
            break
    
    if not paper_id:
        print("没有找到符合条件的试卷，创建一个...")
        resp = requests.post(f'{base_url}/api/admin/papers', json={
            'paper_number': 'APPEAL_TEST_' + str(__import__('time').time()),
            'batch_id': batch_id,
            'question_group_id': qg_id,
            'candidate_name': '复评测试考生',
            'candidate_id': 'APPEAL001'
        }, headers=admin_headers)
        result = resp.json()
        paper_id = result.get('id')
        
        if paper_id:
            resp = requests.put(f'{base_url}/api/admin/papers/{paper_id}', json={
                'current_status': 'finalized'
            }, headers=admin_headers)
            print(f"创建测试试卷成功, ID: {paper_id}")
        else:
            print(f"创建试卷失败: {result}")
    
    print(f"\n使用试卷ID: {paper_id}")

    print(f"\n{'='*60}")
    print(f"  步骤2: 创建复评申请")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/appeals?paper_id={paper_id}&status=pending', headers=admin_headers)
    pending_appeals = resp.json().get('items', [])
    
    if pending_appeals:
        appeal_id = pending_appeals[0]['id']
        print(f"找到进行中的复评申请, ID: {appeal_id}")
    else:
        resp = requests.post(f'{base_url}/api/appeals', json={
            'paper_id': paper_id,
            'appeal_type': 'quality_check',
            'priority': 'high',
            'reason': '质量抽查 - 随机抽取试卷进行复评',
            'description': '本试卷为随机抽取的质量抽查样本，需要重新复核评分质量。'
        }, headers=admin_headers)
        print_result("创建复评申请", resp)
        appeal_id = resp.json().get('id')
        print(f"复评申请ID: {appeal_id}")

    print(f"\n{'='*60}")
    print(f"  步骤3: 查看复评申请列表")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/appeals?page=1&per_page=10', headers=admin_headers)
    print_result("复评申请列表", resp)

    print(f"\n{'='*60}")
    print(f"  步骤4: 查看复评申请详情")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/appeals/{appeal_id}', headers=admin_headers)
    print_result("复评申请详情", resp)

    print(f"\n{'='*60}")
    print(f"  步骤5: 查看复评申请摘要统计")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/appeals/summary', headers=admin_headers)
    print_result("复评申请摘要", resp)

    print(f"\n{'='*60}")
    print(f"  步骤6: 受理复评申请")
    print(f"{'='*60}")
    
    resp = requests.post(f'{base_url}/api/appeals/{appeal_id}/accept', json={
        'remark': '已受理，安排复核员进行复评'
    }, headers=admin_headers)
    print_result("受理复评申请", resp)

    print(f"\n{'='*60}")
    print(f"  步骤7: 开始复评（分配任务）")
    print(f"{'='*60}")
    
    resp = requests.post(f'{base_url}/api/appeals/{appeal_id}/start', json={
        'assignee_id': 4,
        'task_type': 'audit',
        'remark': '分配给复核员甲进行复评'
    }, headers=admin_headers)
    print_result("开始复评", resp)

    print(f"\n{'='*60}")
    print(f"  步骤8: 查看试卷的复评标记")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/query/papers?page=1&per_page=5', headers=admin_headers)
    papers_data = resp.json().get('items', [])
    for p in papers_data:
        if p['id'] == paper_id:
            print(f"试卷: {p['paper_number']}")
            print(f"  状态: {p.get('status_name')}")
            print(f"  是否复评中: {p.get('is_reviewing')}")
            print(f"  复评次数: {p.get('appeal_count')}")
            print(f"  当前复评状态: {p.get('appeal_status_name')}")
            print(f"  复评类型: {p.get('appeal_type_name')}")
            break

    print(f"\n{'='*60}")
    print(f"  步骤9: 查看任务列表中的复评标记")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/query/tasks?page=1&per_page=5', headers=admin_headers)
    tasks_data = resp.json().get('items', [])
    print(f"最新5个任务:")
    for t in tasks_data[:5]:
        is_review = t.get('is_review_task') or t.get('appeal_id')
        review_mark = "[复评任务]" if is_review else "[普通任务]"
        print(f"  {review_mark} {t.get('task_code')} - {t.get('paper_number')} - {t.get('status_name')}")

    print(f"\n{'='*60}")
    print(f"  步骤10: 查看某试卷的所有复评申请")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/appeals/paper/{paper_id}', headers=admin_headers)
    print_result(f"试卷{paper_id}的复评申请", resp)

    print(f"\n{'='*60}")
    print(f"  步骤11: 查看告警模块中的复评统计")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/alerts/summary', headers=admin_headers)
    summary = resp.json()
    print(f"告警摘要中的复评统计:")
    appeal_stats = summary.get('review_appeal_stats', {})
    print(f"  总申请数: {appeal_stats.get('total_appeals')}")
    print(f"  申请中: {appeal_stats.get('pending_appeals')}")
    print(f"  复评中: {appeal_stats.get('reviewing_appeals')}")
    print(f"  已完成: {appeal_stats.get('completed_appeals')}")
    print(f"  已驳回: {appeal_stats.get('rejected_appeals')}")
    print(f"  高优先级待处理: {appeal_stats.get('high_priority_pending')}")

    print(f"\n{'='*60}")
    print(f"  步骤12: 完成复评申请")
    print(f"{'='*60}")
    
    resp = requests.post(f'{base_url}/api/appeals/{appeal_id}/complete', json={
        'conclusion': '复评完成，确认评分准确，质量合格。',
        'final_score': 85.5
    }, headers=admin_headers)
    print_result("完成复评申请", resp)

    print(f"\n{'='*60}")
    print(f"  步骤13: 验证完成后的状态")
    print(f"{'='*60}")
    
    resp = requests.get(f'{base_url}/api/appeals/{appeal_id}', headers=admin_headers)
    detail = resp.json()
    print(f"复评状态: {detail.get('status_name')}")
    print(f"复评结论: {detail.get('conclusion')}")
    print(f"最终分数: {detail.get('final_score')}")
    print(f"操作日志数: {len(detail.get('logs', []))}")
    print(f"日志记录:")
    for log in detail.get('logs', []):
        print(f"  - {log.get('created_at')}: {log.get('action')} by {log.get('operator_name', '系统')} - {log.get('remark', '')[:30]}")

    print(f"\n{'='*60}")
    print(f"  测试完成！")
    print(f"{'='*60}")

if __name__ == '__main__':
    test()
