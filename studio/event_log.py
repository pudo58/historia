"""Read-only, cursor-paginated Studio journal. Legacy events retain unknown metadata."""
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select

from studio.models import Job, JobEvent, ProductionRun, Project


def router(service):
    api = APIRouter(prefix='/api/studio')

    def selection(project_id=None, job_id=None, run_id=None, scene_id=None, kind=None,
                  stage=None, level=None, q=None, **unused):
        if project_id:
            service.require(Project, project_id)
        if job_id:
            job = service.require(Job, job_id)
            if project_id and job.project_id != project_id:
                raise HTTPException(404, 'Tác vụ không thuộc dự án.')
        statement = select(JobEvent, Job).join(Job, Job.id == JobEvent.job_id)
        if project_id:
            statement = statement.where(Job.project_id == project_id)
        if run_id:
            run = service.require(ProductionRun, run_id)
            if project_id and run.project_id != project_id:
                raise HTTPException(404, 'Lượt sản xuất không thuộc dự án.')
            statement = statement.where(Job.id.in_(list(run.checkpoint.get('jobs', {}).values())))
        for column, value in ((Job.id, job_id), (Job.scene_id, scene_id), (Job.kind, kind)):
            if value:
                statement = statement.where(column == value)
        if stage:
            # Keep historical/unscoped events visible, clearly marked in the viewer.
            statement = statement.where(or_(JobEvent.context['stage'].as_string() == stage,
                                             JobEvent.context['stage'].as_string().is_(None)))
        if level:
            statement = statement.where(JobEvent.context['level'].as_string().in_(level.split(',')))
        if q:
            statement = statement.where(JobEvent.message.icontains(q, autoescape=True))
        return statement

    def serialize(event, job):
        context = event.context or {}
        return {'id': event.id, 'job_id': job.id, 'scene_id': job.scene_id, 'kind': job.kind,
                'scene_title': (job.snapshot.get('scene') or {}).get('title'),
                'created_at': event.created_at, 'message': event.message,
                'level': context.get('level'), 'stage': context.get('stage'),
                'source': context.get('source', 'studio'), 'legacy': event.context is None}

    def filters(run_id: str | None = None, job_id: str | None = None, scene_id: str | None = None,
                kind: str | None = None, stage: str | None = None, level: str | None = None,
                q: str | None = Query(default=None, max_length=500)):
        return {'run_id': run_id, 'job_id': job_id, 'scene_id': scene_id, 'kind': kind, 'stage': stage, 'level': level, 'q': q}

    from fastapi import Depends
    filter_dependency = Depends(filters)

    def page(statement, after, before, limit, tail):
        if after and before:
            raise HTTPException(422, 'Chọn after hoặc before.')
        if after:
            statement = statement.where(JobEvent.id > after)
        if before:
            statement = statement.where(JobEvent.id < before)
        descending = bool(before or (tail and not after))
        with service.sessions() as session:
            rows = session.execute(statement.order_by(JobEvent.id.desc() if descending else JobEvent.id).limit(limit + 1)).all()
            more = len(rows) > limit
            rows = rows[:limit]
            if descending:
                rows.reverse()
            return {'items': [serialize(event, job) for event, job in rows], 'has_more': more}

    @api.get('/projects/{project_id}/events')
    def project_events(project_id: str, after: int = Query(0, ge=0), before: int = Query(0, ge=0),
                       limit: int = Query(200, ge=1, le=500), tail: bool = True, options: dict = filter_dependency):
        return page(selection(project_id=project_id, **options), after, before, limit, tail)

    @api.get('/jobs/{id}/journal')
    def job_journal(id: str, after: int = Query(0, ge=0), before: int = Query(0, ge=0),
                    limit: int = Query(200, ge=1, le=500), tail: bool = True, options: dict = filter_dependency):
        options['job_id'] = id
        return page(selection(**options), after, before, limit, tail)

    def download(statement):
        # Freeze the upper ID so a busy renderer cannot make an export endless.
        with service.sessions() as session:
            last = session.execute(statement.order_by(JobEvent.id.desc()).limit(1)).first()
            upper = last[0].id if last else 0
        def content():
            cursor = 0
            while cursor < upper:
                with service.sessions() as session:
                    rows = session.execute(statement.where(JobEvent.id > cursor, JobEvent.id <= upper)
                                           .order_by(JobEvent.id).limit(500)).all()
                    if not rows:
                        break
                    for event, job in rows:
                        cursor = event.id
                        yield json.dumps(serialize(event, job), ensure_ascii=False) + '\n'
        return StreamingResponse(content(), media_type='application/x-ndjson',
            headers={'Content-Disposition': 'attachment; filename="historia-journal.ndjson"'})

    @api.get('/projects/{project_id}/events/export')
    def project_export(project_id: str, options: dict = filter_dependency):
        return download(selection(project_id=project_id, **options))

    @api.get('/jobs/{id}/journal/export')
    def job_export(id: str, options: dict = filter_dependency):
        options['job_id'] = id
        return download(selection(**options))

    return api
