from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.generic import DetailView
from django.views.generic.edit import UpdateView

import reversion

from .models import ApprovalStatus
from .models import Comrade

# If the logged-in user doesn't have a Comrade object, redirect them to
# create one and then come back to the current page.
#
# Note that LoginRequiredMixin must be to the left of this class in the
# view's list of parent classes, and the base View must be to the right.
class ComradeRequiredMixin(object):
    def dispatch(self, request, *args, **kwargs):
        try:
            # Check that the logged-in user has a Comrade instance too:
            # even just trying to access the field will fail if not.
            request.user.comrade
        except Comrade.DoesNotExist:
            # If not, redirect to create one and remember to come back
            # here afterward.
            return HttpResponseRedirect(
                    '{account_url}?{query_string}'.format(
                        account_url=reverse('account'),
                        query_string=urlencode({'next': request.path})))
        return super(ComradeRequiredMixin, self).dispatch(request, *args, **kwargs)

class Preview(DetailView):
    template_name_suffix = ""

    def get_template_names(self):
        name = "{}/preview/{}{}.html".format(
                self.object._meta.app_label,
                self.object._meta.model_name,
                self.template_name_suffix)
        return [name]

class ApprovalStatusAction(LoginRequiredMixin, ComradeRequiredMixin, reversion.views.RevisionMixin, UpdateView):
    template_name_suffix = "_action"

    def notify(self):
        """
        Subclasses can override this to do something, like sending
        email, when the user's action is committed to the database. You
        can check self.prior_status and self.target_status to determine
        what changed.
        """
        pass

    allowed_actions = {
            'approve': ApprovalStatus.APPROVED,
            'reject': ApprovalStatus.REJECTED,
            'withdraw': ApprovalStatus.WITHDRAWN,
            }
    allowed_transitions = frozenset((
            (ApprovalStatus.PENDING, ApprovalStatus.PENDING),
            (ApprovalStatus.WITHDRAWN, ApprovalStatus.PENDING),

            (ApprovalStatus.PENDING, ApprovalStatus.APPROVED),
            (ApprovalStatus.APPROVED, ApprovalStatus.APPROVED),
            (ApprovalStatus.REJECTED, ApprovalStatus.APPROVED),

            (ApprovalStatus.PENDING, ApprovalStatus.WITHDRAWN),
            (ApprovalStatus.APPROVED, ApprovalStatus.WITHDRAWN),

            (ApprovalStatus.PENDING, ApprovalStatus.REJECTED),
            (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED),
            ))

    def get_form_class(self):
        model = self.object.__class__
        action = self.kwargs['action']

        self.prior_status = self.object.approval_status

        if action == 'submit':
            self.target_status = self.prior_status
            # You can re-submit an already-approved request and it will
            # stay approved. In any other case it becomes pending.
            if self.target_status != ApprovalStatus.APPROVED:
                self.target_status = ApprovalStatus.PENDING
        else:
            try:
                self.target_status = self.allowed_actions[action]
            except KeyError:
                raise Http404("Unrecognized action {!r}".format(action))

        if (self.prior_status, self.target_status) not in self.allowed_transitions:
            raise PermissionDenied("Not allowed to {} a {} request.".format(
                action, self.object.get_approval_status_display()))

        if self.prior_status == self.target_status or self.target_status in (ApprovalStatus.PENDING, ApprovalStatus.WITHDRAWN):
            if not self.object.is_submitter(self.request.user):
                raise PermissionDenied("You are not an authorized submitter for this request.")
        elif self.target_status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            if not self.object.is_approver(self.request.user):
                raise PermissionDenied("You are not an authorized approver for this request.")

        if self.target_status in (ApprovalStatus.REJECTED, ApprovalStatus.WITHDRAWN):
            self.fields = ['reason_denied']
        else:
            self.object.reason_denied = ""
            if action != 'submit':
                self.fields = []
        # Otherwise this is a (re-)submit and the subclass should
        # specify which fields need to be edited.

        self.object.approval_status = self.target_status

        return super(ApprovalStatusAction, self).get_form_class()

    def get_template_names(self):
        name = "{}/preview/{}{}.html".format(
                self.object._meta.app_label,
                self.object._meta.model_name,
                self.template_name_suffix)
        return [name]

    def save_form(self, form):
        return form.save()

    def form_valid(self, form):
        reversion.set_comment(self.kwargs['action'].title() + ".")
        self.object = self.save_form(form)

        # Delay calling notify() until the database transaction is fully
        # written to disk.
        transaction.on_commit(self.notify)

        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return self.object.get_preview_url()
