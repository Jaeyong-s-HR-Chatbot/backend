-- 간접 식별(QI) 판정용: 재직자 부서·직급별 인원 (개인정보 칸 없음)
-- ETL에서 매일 실행해 rules/org_counts.csv (칸: dept, position, count) 로 저장
SELECT d.dept_name AS dept, e.position AS position, COUNT(*) AS count
FROM hr.employees e JOIN hr.departments d ON d.dept_id = e.dept_id
WHERE e.status = '재직'
GROUP BY d.dept_name, e.position
UNION ALL
SELECT d.dept_name, e.duty, COUNT(*)
FROM hr.employees e JOIN hr.departments d ON d.dept_id = e.dept_id
WHERE e.status = '재직' AND e.duty IN ('팀장', '본부장', '실장')
GROUP BY d.dept_name, e.duty
ORDER BY 1, 2;
