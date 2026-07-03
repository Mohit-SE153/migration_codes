CREATE TABLE [Production].[ProductSubcategory] (
    [ProductSubcategoryID] int NOT NULL,
    [ProductCategoryID] int NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
