CREATE TABLE [Production].[ProductModel] (
    [ProductModelID] int NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [CatalogDescription] xml NULL,
    [Instructions] xml NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
