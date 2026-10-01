from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.password_validation import validate_password
from .models import User, Vendor, VendorDocument


class AdminUserCreateForm(UserCreationForm):
    role = forms.ChoiceField(choices=[choice for choice in User.Role.choices if choice[0] != User.Role.VENDOR])

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'first_name', 'last_name', 'role', 'phone')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email


class VendorRegisterForm(forms.ModelForm):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput, help_text='Min. 10 characters.')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    class Meta:
        model = Vendor
        fields = ['company_name', 'contact_name', 'phone', 'address', 'tax_id', 'pan', 'category', 'bank_details']
        widgets = {'address': forms.Textarea(attrs={'rows': 2}), 'bank_details': forms.Textarea(attrs={'rows': 2})}

    def clean_email(self):
        e = self.cleaned_data['email'].lower()
        if User.objects.filter(email__iexact=e).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return e

    def clean(self):
        d = super().clean()
        if d.get('password') and d['password'] != d.get('password2'):
            self.add_error('password2', 'Passwords do not match.')
        if d.get('password'):
            validate_password(d['password'])
        return d

    def save(self, commit=True):
        d = self.cleaned_data
        user = User.objects.create_user(username=d['email'], email=d['email'], password=d['password'],
                                        first_name=d['contact_name'], role=User.Role.VENDOR, phone=d['phone'])
        v = super().save(commit=False)
        v.user = user
        v.save()
        return v


class VendorProfileForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = ['contact_name', 'phone', 'address', 'bank_details', 'category']
        widgets = {'address': forms.Textarea(attrs={'rows': 2}), 'bank_details': forms.Textarea(attrs={'rows': 2})}


class VendorDocForm(forms.ModelForm):
    expires_on = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    class Meta:
        model = VendorDocument
        fields = ['title', 'file', 'expires_on']

    def clean_file(self):
        from tenders.validators import validate_upload
        f = self.cleaned_data['file']
        validate_upload(f)
        return f
