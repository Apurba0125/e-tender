from django import forms
from django.utils import timezone

from accounts.models import Vendor
from .models import Approval, Notice, Requisition, Tender
from .validators import validate_upload

DT = dict(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')
DT_FORMATS = ['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M']


class NoticeForm(forms.ModelForm):
    class Meta:
        model = Notice
        fields = ['title', 'description', 'attachment']
        widgets = {'description': forms.Textarea(attrs={'rows': 5})}


class RequisitionForm(forms.ModelForm):
    class Meta:
        model = Requisition
        fields = ['title', 'category', 'description', 'quantity', 'unit', 'estimated_value', 'currency', 'required_by', 'terms']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'terms': forms.Textarea(attrs={'rows': 3}),
            'required_by': forms.DateInput(attrs={'type': 'date'}),
        }


class TenderForm(forms.ModelForm):
    invited = forms.ModelMultipleChoiceField(
        queryset=Vendor.objects.filter(status='APPROVED'), required=False,
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'vendor-checkboxes'}),
        label='Invite vendors (limited tender)')
    bid_start_at = forms.DateTimeField(widget=forms.DateTimeInput(**DT), input_formats=DT_FORMATS)
    bid_end_at = forms.DateTimeField(widget=forms.DateTimeInput(**DT), input_formats=DT_FORMATS, label='Bid deadline')
    opening_at = forms.DateTimeField(widget=forms.DateTimeInput(**DT), input_formats=DT_FORMATS, label='Planned opening')

    class Meta:
        model = Tender
        fields = ['notice', 'title', 'category', 'description', 'quantity', 'unit', 'estimated_value', 'currency',
                  'emd_amount', 'eligibility', 'terms', 'evaluation_criteria', 'contact_person', 'visibility',
                  'bid_start_at', 'bid_end_at', 'opening_at']
        widgets = {k: forms.Textarea(attrs={'rows': 3}) for k in ('description', 'eligibility', 'terms', 'evaluation_criteria')}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        notice_qs = Notice.objects.filter(status='PUBLISHED')
        if self.instance.pk and self.instance.notice_id:
            notice_qs = Notice.objects.filter(pk=self.instance.notice_id) | notice_qs
        self.fields['notice'].queryset = notice_qs.distinct()
        if self.instance.pk:
            self.fields['invited'].initial = Vendor.objects.filter(invites__tender=self.instance)

    def clean(self):
        d = super().clean()
        s, e, o = d.get('bid_start_at'), d.get('bid_end_at'), d.get('opening_at')
        if s and e and e <= s:
            self.add_error('bid_end_at', 'Deadline must be after the start.')
        if e and o and o < e:
            self.add_error('opening_at', 'Opening cannot be before the deadline.')
        if d.get('visibility') == 'LIMITED' and not d.get('invited'):
            self.add_error('invited', 'Select at least one vendor for a limited tender.')
        return d


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    widget = MultiFileInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        for f in files:
            validate_upload(f)
        return files


class TenderDocsForm(forms.Form):
    files = MultiFileField(required=False, label='Attach documents (BOQ, specs, T&C…)')


class BidForm(forms.Form):
    price = forms.DecimalField(min_value=0, max_digits=16, decimal_places=2, label='Quoted price (excl. taxes)')
    taxes = forms.DecimalField(min_value=0, max_digits=16, decimal_places=2, initial=0)
    delivery_days = forms.IntegerField(min_value=1, label='Delivery period (days)')
    validity_days = forms.IntegerField(min_value=1, label='Offer validity (days)', initial=90)
    remarks = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}), label='Technical response / remarks', required=False)
    document = forms.FileField(required=False, label='Technical / supporting document', validators=[validate_upload])
    emd_proof = forms.FileField(required=False, label='EMD / security deposit proof', validators=[validate_upload])
    confirm = forms.BooleanField(label='I confirm this bid is final and binding until the deadline')

    def __init__(self, *a, tender=None, has_emd_proof=False, **kw):
        super().__init__(*a, **kw)
        self.tender, self.has_emd_proof = tender, has_emd_proof

    def clean(self):
        d = super().clean()
        if self.tender and self.tender.emd_amount > 0 and not d.get('emd_proof') and not self.has_emd_proof:
            self.add_error('emd_proof', 'EMD proof is required for this tender.')
        return d


class ForwardForm(forms.Form):
    remarks = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)
    files = MultiFileField(required=False, label='Supporting documents (optional)')


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=Approval.Decision.choices, widget=forms.RadioSelect)
    remarks = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)


class EvaluationForm(forms.Form):
    bid_id = forms.IntegerField(widget=forms.HiddenInput)
    qualified = forms.ChoiceField(choices=[('1', 'Qualified'), ('0', 'Not qualified')], widget=forms.RadioSelect)
    score = forms.DecimalField(required=False, min_value=0, max_value=100, max_digits=5, decimal_places=2)
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))


class ExtendForm(forms.Form):
    new_end = forms.DateTimeField(widget=forms.DateTimeInput(**DT), input_formats=DT_FORMATS, label='New deadline')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}))
