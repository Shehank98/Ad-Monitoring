"""
Reconciliation Agent data models (AGENT_BUILD_BRIEF.md §13, Amendment 01).

These tables belong to the agent only. They reference core models by foreign key
but never change them. Channel and month on ScopeState are copied byte-for-byte
from Schedule (R2) and are never built or re-cased.
"""
from django.conf import settings
from django.db import models

CHANNEL_MAX = 200   # core.Schedule.channel
MONTH_MAX = 50      # core.Schedule.month


class AgentConfig(models.Model):
    """Singleton (pk=1). The kill switch is `enabled`, checked at the start of every
    task and again immediately before every write (gate.py)."""
    INTAKE_MODES = [('off', 'Off'), ('suggest', 'Suggest'), ('auto', 'Auto')]

    enabled = models.BooleanField(default=False)
    autonomy_level = models.PositiveSmallIntegerField(default=0)   # 0..3 (brief §8)
    mapping_threshold = models.FloatField(default=0.92)
    grace_days = models.PositiveSmallIntegerField(default=3)
    upload_debounce_minutes = models.PositiveSmallIntegerField(default=10)   # A5
    tc_intake_mode = models.CharField(max_length=10, choices=INTAKE_MODES, default='off')
    llm_daily_token_cap = models.PositiveIntegerField(default=200_000)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')

    class Meta:
        verbose_name = 'agent configuration'

    def __str__(self):
        return f'AgentConfig(enabled={self.enabled}, level={self.autonomy_level})'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AgentAccountOverride(models.Model):
    """Per-account overrides; NULL = use AgentConfig."""
    account = models.OneToOneField('core.Account', on_delete=models.CASCADE,
                                   related_name='agent_override')
    enabled = models.BooleanField(null=True, blank=True)
    autonomy_level = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(blank=True, default='')

    def __str__(self):
        return f'Override({self.account_id})'


class ScopeState(models.Model):
    STATES = [
        ('STILL_AIRING', 'Still airing'),
        ('WAITING_INPUTS', 'Waiting for inputs'),
        ('STANDALONE', 'Standalone TC'),
        ('INGEST_CHECK', 'Ingestion check'),
        ('MAPPING', 'Needs mapping'),
        ('RECONCILING', 'Reconciling'),
        ('VALIDATING', 'Validating'),
        ('DIAGNOSING', 'Diagnosing'),
        ('NEEDS_HUMAN', 'Needs a person'),
        ('READY_FOR_SIGNOFF', 'Ready for sign-off'),
        ('AUTHORISED', 'Authorised'),
    ]
    account = models.ForeignKey('core.Account', on_delete=models.CASCADE, related_name='agent_scopes')
    channel = models.CharField(max_length=CHANNEL_MAX)   # copied from Schedule, exact
    month = models.CharField(max_length=MONTH_MAX)       # copied from Schedule, exact
    state = models.CharField(max_length=24, choices=STATES, default='WAITING_INPUTS')
    reason = models.CharField(max_length=64, blank=True, default='')
    repair_round = models.PositiveSmallIntegerField(default=0)
    lock_baseline = models.IntegerField(null=True, blank=True)   # V2: multi-flag LMRB rows
    needs_run = models.BooleanField(default=False)
    last_run_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'channel', 'month'],
                                               name='agent_scope_unique')]

    def __str__(self):
        return f'{self.account_id} | {self.channel} | {self.month} [{self.state}]'


class ScheduleStatus(models.Model):
    SUB = [
        ('waiting', 'Waiting'), ('pending_rows', 'Rows pending (Rule 8)'),
        ('reconciled', 'Reconciled'), ('ready', 'Ready'), ('authorised', 'Authorised'),
        ('needs_human', 'Needs a person'),
    ]
    scope = models.ForeignKey(ScopeState, on_delete=models.CASCADE, related_name='schedules')
    schedule = models.OneToOneField('core.Schedule', on_delete=models.CASCADE,
                                    related_name='agent_status')
    sub_status = models.CharField(max_length=16, choices=SUB, default='waiting')
    has_tc = models.BooleanField(default=False)
    # Parent of, or makeup for, another schedule (D25). Drafts (Phase 5) will show:
    # "Makeup spots for this schedule are reported under schedule <number>."
    makeup_linked = models.BooleanField(default=False)
    matched_count = models.PositiveIntegerField(default=0)
    pending_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.schedule_id} [{self.sub_status}]'


class AgentRun(models.Model):
    KINDS = [('cycle', 'Cycle'), ('scope', 'Scope run'), ('dry_run', 'Dry run'),
             ('golden', 'Golden check'), ('audit', 'Core audit')]
    STATUS = [('running', 'Running'), ('ok', 'OK'), ('failed', 'Failed'),
              ('skipped', 'Skipped'), ('rolled_back', 'Rolled back')]
    kind = models.CharField(max_length=10, choices=KINDS)
    scope = models.ForeignKey(ScopeState, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='runs')
    status = models.CharField(max_length=12, choices=STATUS, default='running')
    detail = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.kind} #{self.pk} [{self.status}]'


class _ActionFields(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name='+')
    tier = models.PositiveSmallIntegerField(default=0)
    action_type = models.CharField(max_length=40)
    scope = models.ForeignKey(ScopeState, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    target_model = models.CharField(max_length=60, blank=True, default='')
    target_pk = models.CharField(max_length=40, blank=True, default='')
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True, default='')
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class AgentAction(_ActionFields):
    run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL,
                            related_name='actions')
    reverted_at = models.DateTimeField(null=True, blank=True)
    reverted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='+')

    def __str__(self):
        return f'{self.action_type} T{self.tier} #{self.pk}'


class AgentProposal(_ActionFields):
    KINDS = [('mapping', 'Mapping'), ('alias', 'Column alias'), ('manual_match', 'Manual match'),
             ('tolerance', 'Tolerance'), ('tc_split', 'TC split'), ('tc_link', 'TC link'),
             ('amendment', 'Amendment'), ('finding', 'Finding')]
    STATUS = [('open', 'Open'), ('approved', 'Approved'), ('rejected', 'Rejected'),
              ('applied', 'Applied'), ('superseded', 'Superseded')]
    kind = models.CharField(max_length=14, choices=KINDS)
    status = models.CharField(max_length=12, choices=STATUS, default='open')
    schedule = models.ForeignKey('core.Schedule', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='+')
    confidence = models.FloatField(null=True, blank=True)
    apply_payload = models.JSONField(default=dict, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default='')

    def __str__(self):
        return f'{self.kind} proposal #{self.pk} [{self.status}]'


class SummarySnapshot(models.Model):
    """build_summary_data(..., schedule_id=sid) for ONE schedule (Amendment A4)."""
    KINDS = [('draft', 'Draft'), ('authorised', 'Authorised'), ('golden', 'Golden baseline')]
    scope = models.ForeignKey(ScopeState, on_delete=models.CASCADE, related_name='snapshots')
    schedule = models.ForeignKey('core.Schedule', null=True, on_delete=models.SET_NULL,
                                 related_name='agent_snapshots')
    schedule_number = models.CharField(max_length=50)
    kind = models.CharField(max_length=10, choices=KINDS, default='draft')
    data = models.JSONField()
    sha256 = models.CharField(max_length=64, db_index=True)
    # External-change fingerprint of the account at snapshot time (V5, Phase 1.1)
    fingerprint = models.JSONField(default=dict, blank=True)
    fingerprint_sha256 = models.CharField(max_length=64, blank=True, default='')
    run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL,
                            related_name='snapshots')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'#{self.schedule_number} {self.kind} {self.sha256[:8]}'


class AgentAuthorisation(models.Model):
    """Per schedule (Amendment A4). SummaryReportMeta is never written by the agent."""
    schedule = models.ForeignKey('core.Schedule', on_delete=models.PROTECT,
                                 related_name='agent_authorisations')
    snapshot = models.ForeignKey(SummarySnapshot, on_delete=models.PROTECT,
                                 related_name='authorisations')
    snapshot_sha256 = models.CharField(max_length=64)
    authorised_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                      related_name='+')
    authorised_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['schedule', 'snapshot_sha256'],
                                               name='agent_auth_unique')]

    def __str__(self):
        return f'Authorised #{self.schedule_id} {self.snapshot_sha256[:8]}'


class LlmCall(models.Model):
    purpose = models.CharField(max_length=40)
    scope = models.ForeignKey(ScopeState, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    provider = models.CharField(max_length=20, default='anthropic')
    model = models.CharField(max_length=80, blank=True, default='')
    prompt_version = models.CharField(max_length=40, blank=True, default='')
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=40, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.purpose} {self.outcome}'


class NotificationLog(models.Model):
    dedupe_key = models.CharField(max_length=200, unique=True)   # scope|template|recipient|date
    template_key = models.CharField(max_length=60)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='+')
    scope = models.ForeignKey(ScopeState, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    via = models.CharField(max_length=20, default='whatsapp')
    status = models.CharField(max_length=20, default='queued')
    sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.dedupe_key


class ScopeLockRow(models.Model):
    """Fallback lock for non-PostgreSQL databases (Amendment P2)."""
    key = models.BigIntegerField(unique=True)
    owner = models.CharField(max_length=120)
    acquired_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    def __str__(self):
        return f'lock {self.key} by {self.owner}'


class Heartbeat(models.Model):
    name = models.CharField(max_length=60, unique=True)
    last_beat = models.DateTimeField()
    detail = models.JSONField(default=dict, blank=True)
    alert = models.BooleanField(default=False)          # e.g. service user role check failed
    alert_message = models.TextField(blank=True, default='')

    def __str__(self):
        return f'{self.name} @ {self.last_beat}'
