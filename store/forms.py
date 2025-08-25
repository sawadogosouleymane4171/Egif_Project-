from django import forms
from django.core.exceptions import ValidationError
from .models import Item, Category, Delivery

def validate_image(file):
    """
    Valide le type et la taille de l'image uploadée.
    - formats autorisés : jpeg, png, gif, webp
    - taille max : 5 MB
    """
    content_type = getattr(file, 'content_type', '')
    valid_content_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
    max_size = 5 * 1024 * 1024  # 5 MB

    if content_type not in valid_content_types:
        raise ValidationError('Format d\'image non supporté. Utilisez JPEG, PNG, GIF ou WEBP.')

    if file.size > max_size:
        raise ValidationError('Taille du fichier trop grande. Taille maximale autorisée : 5 MB.')


class ItemForm(forms.ModelForm):
    """
    A form for creating or updating an Item in the inventory.
    """
    image = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(attrs={'class': 'form-control-file'}),
        validators=[validate_image],
        help_text="Image optionnelle (JPEG/PNG/GIF/WEBP | max 5 MB)."
    )
    
    class Meta:
        model = Item
        fields = [
            'name',
            'description',
            'category',
            'quantity',
            'price',
            'expiring_date',
            'vendor',
            'image',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(
                attrs={
                    'class': 'form-control',
                    'rows': 2
                }
            ),
            'category': forms.Select(attrs={'class': 'form-control'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control'}),
            'price': forms.NumberInput(
                attrs={
                    'class': 'form-control',
                    'step': '0.01'
                }
            ),
            'expiring_date': forms.DateTimeInput(
                attrs={
                    'class': 'form-control',
                    'type': 'datetime-local'
                }
            ),
            'vendor': forms.Select(attrs={'class': 'form-control'}),
        }


class CategoryForm(forms.ModelForm):
    """
    A form for creating or updating category.
    """
    class Meta:
        model = Category
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter category name',
                'aria-label': 'Category Name'
            }),
        }
        labels = {
            'name': 'Category Name',
        }


class DeliveryForm(forms.ModelForm):
    class Meta:
        model = Delivery
        fields = [
            'item',
            'customer_name',
            'phone_number',
            'location',
            'date',
            'is_delivered'
        ]
        widgets = {
            'item': forms.Select(attrs={
                'class': 'form-control',
                'placeholder': 'Select item',
            }),
            'customer_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter customer name',
            }),
            'phone_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter phone number',
            }),
            'location': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter delivery location',
            }),
            'date': forms.DateTimeInput(attrs={
                'class': 'form-control',
                'placeholder': 'Select delivery date and time',
                'type': 'datetime-local'
            }),
            'is_delivered': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
                'label': 'Mark as delivered',
            }),
        }
