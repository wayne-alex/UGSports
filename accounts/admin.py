from django.contrib import admin

from accounts.models import AuditLog, NewsComment, NewsPost, Team, Fixture, PhaseSport, Sport, SubCounty, Ward
from accounts.views import User

# Register your models here.
admin.site.register(User)
admin.site.register(AuditLog)
admin.site.register(NewsComment)
admin.site.register(NewsPost)
admin.site.register(Fixture)
admin.site.register(Team)
admin.site.register(PhaseSport)
admin.site.register(Sport)
admin.site.register(SubCounty)
admin.site.register(Ward)